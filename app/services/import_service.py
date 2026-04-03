import csv
import io
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.statement import FileType, Statement, StatementLine, StatementStatus
from app.models.transaction import Transaction


def parse_csv_preview(content: str, max_rows: int = 10) -> dict:
    """Parse CSV content and return headers + preview rows."""
    sniffer = csv.Sniffer()
    try:
        dialect = sniffer.sniff(content[:4096])
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(content), dialect)
    rows = list(reader)
    if not rows:
        return {"headers": [], "rows": [], "delimiter": ","}

    return {
        "headers": rows[0],
        "rows": rows[1:max_rows + 1],
        "total_rows": len(rows) - 1,
        "delimiter": dialect.delimiter,
    }


def parse_csv_transactions(
    content: str, date_col: int, amount_col: int,
    desc_col: int, ref_col: int | None = None,
    date_format: str = "%Y-%m-%d",
) -> list[dict]:
    """Parse CSV into list of transaction dicts."""
    sniffer = csv.Sniffer()
    try:
        dialect = sniffer.sniff(content[:4096])
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(content), dialect)
    headers = next(reader, None)
    if not headers:
        return []

    transactions = []
    for row in reader:
        if len(row) <= max(date_col, amount_col, desc_col):
            continue
        try:
            dt = datetime.strptime(row[date_col].strip(), date_format).date()
        except ValueError:
            for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d-%m-%Y"):
                try:
                    dt = datetime.strptime(row[date_col].strip(), fmt).date()
                    break
                except ValueError:
                    continue
            else:
                continue

        try:
            amount_str = row[amount_col].strip().replace(",", "").replace("$", "").replace("£", "").replace("€", "")
            amount = Decimal(amount_str)
        except (InvalidOperation, ValueError):
            continue

        ref = row[ref_col].strip() if ref_col is not None and ref_col < len(row) else None

        transactions.append({
            "date": dt,
            "amount": amount,
            "description": row[desc_col].strip(),
            "reference": ref,
        })

    return transactions


def parse_ofx(content: bytes) -> list[dict]:
    """Parse OFX file content into list of transaction dicts."""
    from ofxparse import OfxParser
    ofx = OfxParser.parse(io.BytesIO(content))
    transactions = []
    for account in ofx.accounts:
        for tx in account.statement.transactions:
            transactions.append({
                "date": tx.date.date() if hasattr(tx.date, "date") else tx.date,
                "amount": Decimal(str(tx.amount)),
                "description": tx.memo or tx.payee or "",
                "reference": tx.id or None,
            })
    return transactions


async def find_duplicates(
    db: AsyncSession, user_id: uuid.UUID, account_id: uuid.UUID,
    transactions: list[dict],
) -> list[dict]:
    """Mark transactions as potential duplicates if matching existing records."""
    for tx in transactions:
        stmt = select(Transaction).where(
            and_(
                Transaction.user_id == user_id,
                Transaction.account_id == account_id,
                Transaction.date == tx["date"],
                Transaction.amount == tx["amount"],
            )
        )
        result = await db.execute(stmt)
        existing = result.scalars().all()
        tx["is_duplicate"] = False
        for ex in existing:
            if ex.description.lower().strip() == tx["description"].lower().strip():
                tx["is_duplicate"] = True
                break
    return transactions


async def create_statement(
    db: AsyncSession, user_id: uuid.UUID, account_id: uuid.UUID,
    filename: str, file_type: FileType, parsed_transactions: list[dict],
) -> Statement:
    dates = [t["date"] for t in parsed_transactions if t.get("date")]
    stmt = Statement(
        user_id=user_id, account_id=account_id,
        filename=filename, file_type=file_type,
        start_date=min(dates) if dates else None,
        end_date=max(dates) if dates else None,
        record_count=len(parsed_transactions),
        status=StatementStatus.PENDING,
    )
    db.add(stmt)
    await db.flush()

    for tx_data in parsed_transactions:
        line = StatementLine(
            statement_id=stmt.id,
            date=tx_data["date"],
            amount=tx_data["amount"],
            description=tx_data["description"],
            reference=tx_data.get("reference"),
        )
        db.add(line)

    await db.flush()

    result = await db.execute(
        select(Statement)
        .where(Statement.id == stmt.id)
        .options(selectinload(Statement.lines))
    )
    return result.scalar_one()


async def import_statement_lines(
    db: AsyncSession, user_id: uuid.UUID, statement_id: uuid.UUID,
    line_ids: list[uuid.UUID], account_id: uuid.UUID,
) -> int:
    """Import selected statement lines as transactions."""
    count = 0
    for lid in line_ids:
        line = await db.get(StatementLine, lid)
        if not line:
            continue
        tx = Transaction(
            user_id=user_id, account_id=account_id,
            date=line.date, amount=line.amount,
            description=line.description,
            original_description=line.description,
            reference=line.reference,
            statement_line_id=line.id,
        )
        db.add(tx)
        count += 1

    stmt = await db.get(Statement, statement_id)
    if stmt:
        stmt.status = StatementStatus.IMPORTED
    await db.flush()
    return count
