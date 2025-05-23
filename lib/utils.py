import time
import math
import random
from typing import Literal

from alpaca.broker import StopOrderRequest
from fastapi import Request
from alpaca.data import Quote
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, CryptoLatestQuoteRequest
from alpaca.trading import Position, Order as AlpacaOrder
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    OrderSide, TimeInForce, OrderStatus, OrderClass)
from alpaca.trading.requests import (
    StopLimitOrderRequest, LimitOrderRequest, MarketOrderRequest, ClosePositionRequest, TakeProfitRequest, StopLossRequest, TrailingStopOrderRequest)

from lib.db import Order
from lib.env_vars import get_accounts, get_account

FINISHED_STATUSES = [OrderStatus.FILLED, OrderStatus.CANCELED,
                     OrderStatus.EXPIRED, OrderStatus.DONE_FOR_DAY]

MAX_WAIT = 30.0


def get_client_ip(request: Request) -> str | list[str]:
    '''Checks for the real client IP address in the request headers from a number of common sources.'''
    headers = [
        'X-Forwarded-For',
        'CF-Connecting-IP',
        'True-Client-IP',
        'X-Client-IP',
        'X-Cluster-Client-IP',
        'X-Forwarded',
        'Forwarded-For',
        'Forwarded',
        'X-Forwarded-Host',
        'X-Real-IP',
        'Fly-Client-IP'
    ]
    for header in headers:
        ip = request.headers.get(header)
        # if there is a comma in the header, it is a list of IPs
        if ip and ',' in ip:
            return [x.strip() for x in ip.split(',')]
        if ip:
            return ip

    return request.client.host


def get_trading_clients() -> dict[str, TradingClient]:
    '''Returns a dictionary using the name as the key and the TradingClient object as the value.'''
    accounts = get_accounts()
    clients = {}
    for name, creds in accounts.items():
        clients[name] = TradingClient(
            creds.api_key, creds.api_secret, paper=creds.paper)
    return clients


def get_trading_client(name: str) -> TradingClient | None:
    creds = get_account(name)
    if not creds:
        return None
    return TradingClient(creds.api_key, creds.api_secret, paper=creds.paper)


def is_extended_hours(client: TradingClient) -> bool:
    '''Returns true if the market is closed but extended hours are active.'''
    clock = client.get_clock()
    if clock.is_open:
        return False

    # check if the time is before 8pm or after 4am
    current_time = clock.timestamp
    return current_time.hour < 20 and current_time.hour >= 4


def can_trade(client: TradingClient) -> bool:
    '''Returns true if the market is open or extended hours are active.'''
    clock = client.get_clock()
    return clock.is_open or is_extended_hours(client)


def exec_trade(client: TradingClient, order: Order, extended_hours: bool = False, wait_for_fill: bool = False) -> AlpacaOrder:
    account = client.get_account()
    # we're doing notional orders, but we need to know how much.
    # If its a stock and not leveraged, get our regular buying power and multiply by the percentage
    # If its a stock and leveraged or crypto, get our non marginable buying power and multiply by the percentage
    buying_power = float(account.buying_power)
    if order.leveraged or order.asset_class == "crypto":
        buying_power = float(account.non_marginable_buying_power)

    # cannot have more than 2 decimal places
    notional = round(buying_power * order.buying_power_pct, 2)
    # if we're doing limit orders, we'll need this.
    qty = math.floor(notional / order.price)

    # if the value is less than a dollar, we can't trade. Throw bad request
    if notional < 1:
        raise Exception("Notional value is less than $1. Cannot trade.")

    order_req = MarketOrderRequest(
        symbol=order.ticker,
        qty=qty,
        time_in_force=TimeInForce.DAY if not order.asset_class == "crypto" else TimeInForce.GTC,
        side=OrderSide.BUY if order.action == "buy" else OrderSide.SELL,
    )

    # if we are doing a limit order, we can't do fractional shares
    if order.trailing_stop or order.sl or order.tp:
        # get the quantity of shares we can buy
        if qty < 1:
            raise Exception(
                "Limit orders must have a integer quantity greater than 0.")
        # still just a standard market order, just using qty instead of notional
        order_req = MarketOrderRequest(
            symbol=order.ticker,
            qty=qty,
            time_in_force=TimeInForce.DAY if not order.asset_class == "crypto" else TimeInForce.GTC,
            side=OrderSide.BUY if order.action == "buy" else OrderSide.SELL,
        )

    # need to finish the logic for sl, tp, and trailing_sl
    # if we have both, we need to use a bracket order
    # However for the rest of the logic, we need to have two separate orders.
    # First order is a standard limit or market order. Then we need to create the exit order.
    # if we have a trailing_sl, we need to use a trailing stop order
    # if we only have sl, we need to use a stop limit order
    # if we only have tp, we need to use a limit order
    # TODO properly implement and test this logic
    if not extended_hours:
        if order.tp and order.sl:
            if qty < 1:
                raise Exception(
                    "Limit orders must have a integer quantity greater than 0.")
            stop_price = round(order.price * (1 - order.sl), 2)
            limit_price = round(order.price * (1 + order.tp), 2)
            stop_loss = StopLossRequest(stop_price=stop_price)
            take_profit = TakeProfitRequest(limit_price=limit_price)
            order_req = MarketOrderRequest(
                symbol=order.ticker,
                qty=qty,
                time_in_force=TimeInForce.GTC,
                side=OrderSide.BUY if order.action == "buy" else OrderSide.SELL,
                stop_loss=stop_loss,
                take_profit=take_profit,
                order_class=OrderClass.BRACKET
            )

    if extended_hours and order.asset_class != "crypto":
        qty = math.floor(notional / order.price)
        if qty < 1:
            raise Exception(
                "Limit orders must have a integer quantity greater than 0.")
        order_req = LimitOrderRequest(
            extended_hours=True,
            symbol=order.ticker,
            qty=qty,
            time_in_force=TimeInForce.DAY,
            side=OrderSide.BUY if order.action == "buy" else OrderSide.SELL,
            limit_price=order.high or order.price,
        )

    # if we have a stop loss, take profit, or trailing stop, we need to create a separate order
    # However this shouldn't execute if the order request is a bracket order or its a sell order
    if order_req.order_class != OrderClass.BRACKET and order.action == "buy":
        # if there is an error in here, liquidate the newly opened position
        # and raise the error
        try:
            if order.sl or order.tp or order.trailing_stop:
                # first we need to create the initial order and wait for it to fill
                alpaca_order = client.submit_order(order_req)
                start = time.time()
                while alpaca_order.status not in FINISHED_STATUSES:
                    if alpaca_order.status != OrderStatus.NEW:
                        time.sleep(0.25)
                    alpaca_order = client.get_order_by_id(alpaca_order.id)
                    if time.time() - start >= MAX_WAIT:
                        break
                # if the order is not filled, we need to cancel it and return
                if alpaca_order.status != OrderStatus.FILLED:
                    client.cancel_order_by_id(alpaca_order.id)
                    alpaca_order.status = OrderStatus.CANCELED
                    return alpaca_order

                # now is the logic for the rest of the orders
                # we will overwrite the order_req with the new order and let the logic continue
                # we'll start with the stop loss
                if order.sl:
                    stop_price = round(
                        float(alpaca_order.filled_avg_price) * (1 - order.sl), 2)
                    order_req = StopOrderRequest(
                        symbol=alpaca_order.symbol,
                        qty=alpaca_order.filled_qty,
                        time_in_force=TimeInForce.GTC,
                        side=OrderSide.SELL,
                        stop_price=stop_price,
                    )
                elif order.tp:
                    limit_price = round(
                        float(alpaca_order.filled_avg_price) * (1 + order.tp), 2)
                    stop_price = round(
                        float(alpaca_order.filled_avg_price) * (1 - order.tp), 2)
                    order_req = StopLimitOrderRequest(
                        symbol=alpaca_order.symbol,
                        qty=alpaca_order.filled_qty,
                        time_in_force=TimeInForce.GTC,
                        side=OrderSide.SELL,
                        limit_price=limit_price,
                        stop_price=stop_price
                    )
                elif order.trailing_stop:
                    order_req = TrailingStopOrderRequest(
                        symbol=alpaca_order.symbol,
                        qty=alpaca_order.filled_qty,
                        time_in_force=TimeInForce.GTC,
                        side=OrderSide.SELL,
                        # for some reason they want this as a percentage
                        # I want all the inputs to be consistent
                        trail_percent=order.trailing_stop * 100,
                    )
        except Exception as e:
            # if we own the security, liquidate it
            # check the order status
            # if the order is not filled, cancel it
            # if the order is filled, create a market sell order
            # if the order is partially filled, cancel it and create a market sell order
            # otherwise just raise the error
            if alpaca_order.status == OrderStatus.FILLED:
                client.submit_order(MarketOrderRequest(
                    symbol=alpaca_order.symbol,
                    qty=alpaca_order.filled_qty,
                    time_in_force=TimeInForce.GTC,
                    side=OrderSide.SELL,
                ))
            elif alpaca_order.status == OrderStatus.PARTIALLY_FILLED:
                client.cancel_order_by_id(alpaca_order.id)
                client.submit_order(MarketOrderRequest(
                    symbol=alpaca_order.symbol,
                    qty=alpaca_order.filled_qty,
                    time_in_force=TimeInForce.GTC,
                    side=OrderSide.SELL,
                ))

            raise e

    if not wait_for_fill:
        return client.submit_order(order_req)

    alpaca_order = client.submit_order(order_req)

    start = time.time()
    while alpaca_order.status not in FINISHED_STATUSES:
        if alpaca_order.status != OrderStatus.NEW:
            time.sleep(0.25)
        alpaca_order = client.get_order_by_id(alpaca_order.id)
        if time.time() - start >= MAX_WAIT:
            break

    return alpaca_order


def get_current_position(client: TradingClient, ticker: str) -> Position | None:
    position = None
    try:
        position = client.get_open_position(ticker)
    except Exception as e:
        pass
    return position


def close_position(client: TradingClient, ticker: str, percentage: float = 100.0, wait_for_fill: bool = False) -> AlpacaOrder | None:
    position = None
    try:
        position = client.close_position(ticker, ClosePositionRequest(
            percentage=percentage
        ))
        if wait_for_fill:
            start = time.time()
            while position.status not in FINISHED_STATUSES:
                if position.status != OrderStatus.NEW:
                    time.sleep(0.25)
                position = client.get_order_by_id(position.id)
                if time.time() - start >= MAX_WAIT:
                    break
    except Exception as e:
        pass
    return position


def get_latest_quote(ticker: str, asset_class: Literal["stock", "crypto"] = "stock") -> Quote:
    '''Returns the latest quote for the given ticker. If the asset class is crypto, it will use the CryptoHistoricalDataClient, otherwise it will use the StockHistoricalDataClient.'''
    accounts = list(get_accounts().values())
    random_account = random.choice(accounts)

    if asset_class == "crypto":
        client = CryptoHistoricalDataClient(
            random_account.api_key, random_account.api_secret, paper=random_account.paper)
        resp = client.get_crypto_latest_quote(
            CryptoLatestQuoteRequest(symbol_or_symbols=ticker))
        return resp[ticker]

    client = StockHistoricalDataClient(
        random_account.api_key, random_account.api_secret, paper=random_account.paper)
    resp = client.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=ticker))
    return resp[ticker]
