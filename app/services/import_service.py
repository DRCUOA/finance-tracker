import csv
import io
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, and_, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.statement import FileType, Statement, StatementLine, StatementStatus
from app.models.transaction import Transaction


@dataclass
class ImportResult:
    imported: int = 0
    skipped: int = 0
    skipped_descriptions: list[str] = field(default_factory=list)


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


async def _is_duplicate(
    db: AsyncSession, user_id: uuid.UUID, account_id: uuid.UUID,
    tx_date: date, amount: Decimal, description: str,
    reference: str | None,
) -> bool:
    """Check whether a transaction already exists in the database."""
    if reference:
        stmt = select(Transaction.id).where(
            and_(
                Transaction.account_id == account_id,
                Transaction.reference == reference,
            )
        ).limit(1)
        result = await db.execute(stmt)
        if result.first():
            return True

    stmt = select(Transaction.id).where(
        and_(
            Transaction.user_id == user_id,
            Transaction.account_id == account_id,
            Transaction.date == tx_date,
            Transaction.amount == amount,
            sa_func.lower(sa_func.trim(Transaction.description))
            == description.lower().strip(),
        )
    ).limit(1)
    result = await db.execute(stmt)
    return result.first() is not None


async def find_duplicates(
    db: AsyncSession, user_id: uuid.UUID, account_id: uuid.UUID,
    transactions: list[dict],
) -> list[dict]:
    """Mark transactions as potential duplicates if matching existing records."""
    refs = [t["reference"] for t in transactions if t.get("reference")]
    existing_refs: set[str] = set()
    if refs:
        stmt = select(Transaction.reference).where(
            and_(
                Transaction.account_id == account_id,
                Transaction.reference.in_(refs),
            )
        )
        result = await db.execute(stmt)
        existing_refs = {row[0] for row in result.all()}

    for tx in transactions:
        ref = tx.get("reference")
        if ref and ref in existing_refs:
            tx["is_duplicate"] = True
            continue

        stmt = select(Transaction.id).where(
            and_(
                Transaction.user_id == user_id,
                Transaction.account_id == account_id,
                Transaction.date == tx["date"],
                Transaction.amount == tx["amount"],
                sa_func.lower(sa_func.trim(Transaction.description))
                == tx["description"].lower().strip(),
            )
        ).limit(1)
        result = await db.execute(stmt)
        tx["is_duplicate"] = result.first() is not None

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
) -> ImportResult:
    """Import selected statement lines, skipping duplicates."""
    result = ImportResult()
    for lid in line_ids:
        line = await db.get(StatementLine, lid)
        if not line:
            continue

        if await _is_duplicate(
            db, user_id, account_id,
            line.date, line.amount, line.description, line.reference,
        ):
            result.skipped += 1
            result.skipped_descriptions.append(line.description)
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
        await db.flush()
        result.imported += 1

    stmt = await db.get(Statement, statement_id)
    if stmt:
        stmt.status = StatementStatus.IMPORTED
    await db.flush()
    return result
