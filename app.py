from typing import Annotated
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlmodel import Session, select

from lib.constants import ORIGINS, WHITELIST as IP_WHITELIST
from lib.db import get_session, create_db_and_tables, AccountSnapshot, Order
from lib.env_vars import get_accounts, TEST_MODE
from lib.utils import (exec_trade, get_client_ip, get_current_position, close_position, get_latest_quote,
                       get_trading_client, is_extended_hours)


SessionDep = Annotated[Session, Depends(get_session)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    '''Creates a lifespan for items that should be run at startup and shutdown.
Startup tasks should be placed before the yield, and shutdown tasks should be placed after the yield.'''
    create_db_and_tables()
    yield


app = FastAPI(name="Webhook Trader", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get('/account/{name}')
async def get_account(name: str):
    client = get_trading_client(name)
    if not client:
        # return a 404
        return JSONResponse(content={"error": "Account not found"}, status_code=status.HTTP_404_NOT_FOUND)
    account = client.get_account()
    return account


@app.get("/snapshots", response_model=list[AccountSnapshot])
async def get_snapshots(session: SessionDep):
    # Get the last 12 snapshots for each account
    limit = 12 * len(get_accounts())
    statement = select(AccountSnapshot).order_by(
        AccountSnapshot.created_at.desc()).limit(limit)
    snapshots = session.exec(statement).all()
    return snapshots


@app.get("/snapshot/{name}", response_model=AccountSnapshot)
async def get_snapshot(name: str, session: SessionDep):
    # create and return a snapshot for the account
    # save the new snapshot to the database
    client = get_trading_client(name)
    account = client.get_account()
    snapshot = AccountSnapshot(
        account_id=str(account.id),
        name=name,
        cash=float(account.cash),
        equity=float(account.equity),
    )
    session.add(snapshot)
    session.commit()
    session.refresh(snapshot)
    return snapshot


@app.post("/webhook/{name}")
async def webhook(name: str, order: Order, session: SessionDep, req: Request):
    # first, check if the IP is in the whitelist
    ip = get_client_ip(req)
    if ip not in IP_WHITELIST:
        return JSONResponse(content={"error": "IP not in whitelist"}, status_code=status.HTTP_401_UNAUTHORIZED)
    if not order.nickname:
        order.nickname = name
    if not order.max_slippage:
        order.max_slippage = 0
    # log the order
    session.add(order)
    session.commit()
    # if we're in test mode, we're done. Echo the order back.
    if TEST_MODE:
        session.refresh(order)
        return order

    # otherwise, we need to forward to alpaca and add the order ID to the order
    client = get_trading_client(name)
    # the webhooks should only fire if we're in extended hours or the market is open
    # so we don't need to check if we can trade
    # we do need to check if we're in extended hours, as the order type will be different
    extended_hours = is_extended_hours(client)

    # check if we've hit 3 day trades with equity under $25k
    # this is needed because we can always buy, but selling gets restricted if we hit the limit
    # so we should prevent the order from going through so that we're not in a position where we can't sell
    account = client.get_account()
    if account.daytrade_count >= 3 and float(account.equity) < 25_000:
        return JSONResponse(content={"error": "Pattern day trader"}, status_code=status.HTTP_429_TOO_MANY_REQUESTS)

    position = get_current_position(client, order.ticker)
    # if we don't hold the position, simply long or short the position
    if not position:
        if order.max_slippage > 0:
            # do a slippage check
            quote = get_latest_quote(order.ticker, order.asset_class)
            # if we are selling (shorting), use bid
            # if we are buying (longing), use ask
            price = quote.bid_price if order.action == "sell" else quote.ask_price
            # get the absolute value of the slippage
            slippage = abs((price - order.price) / order.price)
            if slippage > order.max_slippage:
                return JSONResponse(content={"error": "Slippage too high"}, status_code=status.HTTP_412_PRECONDITION_FAILED)

        new_order = exec_trade(client, order, extended_hours)
        order.order_id = str(new_order.id)
        session.commit()
        session.refresh(order)
        return order
    else:
        # if the position is flat, or the same as what we already have, we're done
        if position.side == order.market_position or order.market_position == "flat":
            return order

        # at this point, we have to close the position and open a new one regardless of the market position
        close_position(client, order.ticker, wait_for_fill=True)
        # once the existing position is closed, we can open a new one
        new_order = exec_trade(client, order, extended_hours)
        order.order_id = str(new_order.id)
        session.commit()
        session.refresh(order)
        return order

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
