from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.templating import BASE_DIR
from app.routers import (
    accounts,
    auth,
    backup,
    bank_feeds,
    categories,
    dashboard,
    help,
    imports,
    matching_rules,
    reconciliation,
    reports,
    spending,
    sql_tool,
    transactions,
)

app = FastAPI(title="Finance Tracker")

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard", status_code=302)


app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(spending.router)
app.include_router(accounts.router)
app.include_router(categories.router)
app.include_router(matching_rules.router)
app.include_router(transactions.router)
app.include_router(imports.router)
app.include_router(bank_feeds.router)
app.include_router(reconciliation.router)
app.include_router(reports.router)
app.include_router(sql_tool.router)
app.include_router(backup.router)
app.include_router(help.router)
