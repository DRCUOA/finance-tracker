import re
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MAX_ROWS = 500

_FORBIDDEN_KW = re.compile(
    r"\b("
    r"INSERT|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|COPY|EXECUTE|EXEC|"
    r"CALL|SET\s+ROLE|UNION|INTERSECT|EXCEPT|JOIN|"
    r"INFORMATION_SCHEMA|PG_CATALOG|PG_"
    r")\b",
    re.IGNORECASE,
)

_OTHER_TABLES = re.compile(
    r"\b("
    r"users|accounts|categories|budgets|statements|statement_lines|"
    r"reconciliations|matching_rules|alembic_version|refresh_tokens"
    r")\b",
    re.IGNORECASE,
)

_UNSAFE_UPDATE_COLS = re.compile(
    r"\b(user_id|id)\s*=", re.IGNORECASE
)


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    row_count: int
    statement_type: str
    truncated: bool = False


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", "", sql)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return sql.strip()


def validate_query(sql: str) -> tuple[str, str]:
    """Validate and return (statement_type, cleaned_sql). Raises ValueError on bad input."""
    cleaned = _strip_comments(sql)
    if not cleaned:
        raise ValueError("Empty query")

    parts = [s.strip() for s in cleaned.split(";") if s.strip()]
    if len(parts) > 1:
        raise ValueError("Multiple statements are not allowed")
    cleaned = parts[0]

    upper = cleaned.lstrip().upper()
    if upper.startswith("SELECT"):
        stmt_type = "SELECT"
    elif upper.startswith("UPDATE"):
        stmt_type = "UPDATE"
    elif upper.startswith("DELETE"):
        stmt_type = "DELETE"
    else:
        raise ValueError("Only SELECT, UPDATE, and DELETE are allowed")

    if _FORBIDDEN_KW.search(cleaned):
        kw = _FORBIDDEN_KW.search(cleaned).group()
        raise ValueError(f"Forbidden keyword: {kw}")

    if re.search(r"\(\s*SELECT\b", cleaned, re.IGNORECASE):
        raise ValueError("Subqueries are not allowed")

    if stmt_type == "SELECT":
        if not re.search(r"\bFROM\s+transactions\b", cleaned, re.IGNORECASE):
            raise ValueError("SELECT must query the transactions table")
    elif stmt_type == "UPDATE":
        if not re.search(r"\bUPDATE\s+transactions\b", cleaned, re.IGNORECASE):
            raise ValueError("UPDATE must target the transactions table")
        set_match = re.search(r"\bSET\b(.+?)(\bWHERE\b|$)", cleaned, re.IGNORECASE | re.DOTALL)
        if set_match and _UNSAFE_UPDATE_COLS.search(set_match.group(1)):
            raise ValueError("Cannot modify id or user_id columns")
    elif stmt_type == "DELETE":
        if not re.search(r"\bDELETE\s+FROM\s+transactions\b", cleaned, re.IGNORECASE):
            raise ValueError("DELETE must target the transactions table")

    if _OTHER_TABLES.search(cleaned):
        tbl = _OTHER_TABLES.search(cleaned).group()
        raise ValueError(f"Only the transactions table is accessible (found reference to: {tbl})")

    return stmt_type, cleaned


def _inject_user_filter(sql: str, stmt_type: str) -> str:
    if re.search(r"\bWHERE\b", sql, re.IGNORECASE):
        sql = re.sub(
            r"\bWHERE\b",
            "WHERE user_id = :user_id AND",
            sql,
            count=1,
            flags=re.IGNORECASE,
        )
    else:
        if stmt_type == "SELECT":
            anchor = re.search(
                r"\b(ORDER\s+BY|GROUP\s+BY|HAVING|LIMIT)\b", sql, re.IGNORECASE
            )
            if anchor:
                pos = anchor.start()
                sql = sql[:pos] + " WHERE user_id = :user_id " + sql[pos:]
            else:
                sql += " WHERE user_id = :user_id"
        else:
            sql += " WHERE user_id = :user_id"

    if stmt_type == "SELECT" and not re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
        sql += f" LIMIT {MAX_ROWS}"

    return sql


async def execute_query(
    db: AsyncSession, user_id: uuid.UUID, raw_sql: str
) -> QueryResult:
    stmt_type, cleaned = validate_query(raw_sql)
    final_sql = _inject_user_filter(cleaned, stmt_type)

    result = await db.execute(text(final_sql), {"user_id": user_id})

    if stmt_type == "SELECT":
        columns = list(result.keys())
        all_rows = result.fetchall()
        truncated = len(all_rows) >= MAX_ROWS
        rows = [list(r) for r in all_rows]
        for row in rows:
            for i, val in enumerate(row):
                if isinstance(val, uuid.UUID):
                    row[i] = str(val)
                elif hasattr(val, "isoformat"):
                    row[i] = val.isoformat()
                elif isinstance(val, (int, float, bool, str)) or val is None:
                    pass
                else:
                    row[i] = str(val)
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            statement_type=stmt_type,
            truncated=truncated,
        )
    else:
        await db.flush()
        affected = result.rowcount
        return QueryResult(
            columns=[],
            rows=[],
            row_count=affected,
            statement_type=stmt_type,
        )
