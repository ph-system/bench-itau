from __future__ import annotations

import os
import re
import unicodedata
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Callable

import pandas as pd
import pyodbc
from dotenv import load_dotenv


def load_env_files() -> None:
    root_dir = Path(__file__).resolve().parents[1]
    load_dotenv(root_dir / ".env")
    load_dotenv(root_dir / "ETL" / ".env")


load_env_files()

SERVER = os.getenv("DB_SERVER")
DATABASE = os.getenv("DB_NAME")
USER = os.getenv("DB_USER")
PASSWORD = os.getenv("DB_PASSWORD")
DRIVER_ENV = os.getenv("DB_DRIVER")

TABLE = "dbo.contencion_itau_vencida"
BATCH_SIZE = 10000

COLUMN_SPECS = [
    ("N", "integer"), ("NOMBRE", "text100"), ("OPER", "operation"), ("RUT", "integer"),
    ("DV1", "text1"), ("GLOSA_TIPO_CARTERA", "text25"), ("SEGMENTO", "text25"),
    ("DESC_PRODUCTO", "text50"), ("PRODUCTO", "text10"), ("FASE_PROY_MAX", "integer"),
    ("CANAL", "text50"), ("GESTOR", "text50"), ("SUPERVISOR", "text50"),
    ("COBRADOR", "text50"), ("EJEC_NORM", "integer"), ("ZONA", "text50"),
    ("SUCURSAL", "text50"), ("EJECUTIVO_BANCO", "text75"), ("DM_INI", "integer"),
    ("DM_ACT", "integer"), ("DM_MAX", "integer"), ("SALDO_INI", "integer"),
    ("TRAMO_SALDO", "text25"), ("SALDO_ACT", "integer"), ("SALDO_CONT", "integer"),
    ("SALDO_NORMALIZADO_COPIA", "integer"), ("EFECT_RECUPERADO", "integer"),
    ("RENE_MES", "integer"), ("FLAG_CAMPANA", "integer"), ("EN_AGIR", "text2"),
    ("DETALLE_MARCA", "text100"), ("FLAG_EXCLUSION", "integer"), ("EXCLUSION_GLOSA", "text2"),
    ("PILOTO_ASIG", "text2"), ("FLAG_GGEE", "integer"), ("FLAG_GGEE_NOTIF", "integer"),
    ("DETALLE_GGEE", "text50"), ("ESTADO_JUDICIAL", "text50"), ("FLAG_CAMPANA_2", "integer"),
    ("TIPO_CAMPANA", "text25"), ("ESTADO_CURSE_CAMPANA", "integer"),
]

COLUMN_KINDS = dict(COLUMN_SPECS)
EXPECTED_COLUMNS = [name for name, _ in COLUMN_SPECS]


def pick_driver() -> str:
    available = list(pyodbc.drivers())
    preferred = [DRIVER_ENV] if DRIVER_ENV else []
    preferred += ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server", "SQL Server"]
    for driver in preferred:
        if driver in available:
            return driver
    raise RuntimeError(f"No hay driver ODBC para SQL Server. Drivers encontrados: {available}")


def connect() -> pyodbc.Connection:
    missing = [
        name
        for name, value in {
            "DB_SERVER": SERVER,
            "DB_NAME": DATABASE,
            "DB_USER": USER,
            "DB_PASSWORD": PASSWORD,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError("Faltan variables en .env: " + ", ".join(missing))

    driver = pick_driver()
    conn_str = (
        f"Driver={{{driver}}};"
        f"Server={SERVER};"
        f"Database={DATABASE};"
        f"Uid={USER};"
        f"Pwd={PASSWORD};"
        "TrustServerCertificate=yes;"
        "Encrypt=yes;"
    )
    return pyodbc.connect(conn_str, timeout=30)


def sql_ident(name: str) -> str:
    return "[" + str(name).replace("]", "]]" ) + "]"


def normalize_excel_col(value: object) -> str:
    text = str(value).replace("\x00", "").replace("\ufeff", "").strip().upper()
    text = re.sub(r"\.\d+$", "", text)
    if re.fullmatch(r"N[°º�]?", text):
        return "N"

    text = text.replace("�", "N")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("°", "").replace("º", "")
    text = re.sub(r"[^A-Z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


def normalize_excel_columns(columns: pd.Index) -> list[str]:
    counts: dict[str, int] = {}
    normalized = []

    for column in columns:
        name = normalize_excel_col(column)
        counts[name] = counts.get(name, 0) + 1
        normalized.append(name if counts[name] == 1 else f"{name}_{counts[name]}")

    return normalized


def clean_cell(value: object) -> str | None:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).replace("\x00", "").strip()
    if not text or text.lower() == "nan":
        return None
    return text


def clean_text(value: object) -> str | None:
    text = clean_cell(value)
    if text is None:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def clean_integer(value: object) -> str | None:
    text = clean_cell(value)
    if text is None:
        return None

    text = re.sub(r"[^0-9,\.\-]", "", text)
    if text in {"", "-", ".", ","}:
        return None

    if "." in text and "," in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", "" if text.count(",") > 1 else ".")
    elif text.count(".") > 1:
        text = text.replace(".", "")

    try:
        return str(int(Decimal(text)))
    except Exception:
        return None


def clean_operation(value: object) -> str | None:
    text = clean_cell(value)
    if text is None:
        return None

    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        number = clean_integer(value)
        return number.zfill(24) if number else None

    if re.fullmatch(r"\d+(?:\.0+)?", text):
        text = text.split(".", 1)[0]
    else:
        text = re.sub(r"\D", "", text)

    if not text:
        return None
    return text[-24:].zfill(24)


def sql_type_for(kind: str) -> str:
    if kind == "integer":
        return "DECIMAL(38,0) NULL"
    if kind == "operation":
        return "NVARCHAR(24) NULL"
    if kind.startswith("text") and kind[4:].isdigit():
        return f"NVARCHAR({kind[4:]}) NULL"
    return "NVARCHAR(MAX) NULL"


def value_for_insert(column: str, value: object):
    kind = COLUMN_KINDS[column]
    if kind == "integer":
        return clean_integer(value)
    if kind == "operation":
        return clean_operation(value)
    return clean_text(value)


def read_excel(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"No existe el Excel en: {path}")

    raw = path.read_bytes()
    try:
        excel = pd.ExcelFile(BytesIO(raw), engine="openpyxl")
    except Exception as error:
        raise ValueError(
            "El archivo no es un Excel .xlsx valido para Itau Vencida o esta danado. "
            f"Detalle: {error}"
        ) from error
    hoja = next((s for s in excel.sheet_names if "detalle" in s.strip().lower()), None)

    if hoja is None:
        raise ValueError(
            "El archivo no tiene la estructura esperada para Itau Vencida. "
            f"No se encontro la hoja 'Detalle'. Hojas disponibles: {excel.sheet_names}"
        )

    try:
        df = pd.read_excel(BytesIO(raw), sheet_name=hoja, dtype=object, engine="openpyxl")
    except Exception as error:
        raise ValueError(
            "No fue posible leer la hoja Detalle del Excel de Itau Vencida. "
            f"Detalle: {error}"
        ) from error
    df.columns = normalize_excel_columns(df.columns)
    df = df.where(pd.notnull(df), None)

    missing = [col for col in EXPECTED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(
            "El archivo no tiene la estructura esperada para Itau Vencida. "
            "Faltan columnas: " + ", ".join(missing)
        )

    df = df[EXPECTED_COLUMNS].copy()
    df["NOMBRE"] = df["NOMBRE"].ffill()
    return df


def ensure_table() -> None:
    column_definitions = ",\n                    ".join(
        f"{sql_ident(name)} {sql_type_for(kind)}" for name, kind in COLUMN_SPECS
    )

    with connect() as cn:
        cur = cn.cursor()
        cur.execute(
            f"""
            IF OBJECT_ID('{TABLE}', 'U') IS NULL
            BEGIN
                CREATE TABLE {TABLE} (
                    id BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    fecha_carga DATE NOT NULL CONSTRAINT DF_contencion_itau_vencida_fecha_carga DEFAULT (CONVERT(date, GETDATE())),
                    ts_carga DATETIME2(0) NOT NULL CONSTRAINT DF_contencion_itau_vencida_ts_carga DEFAULT (SYSDATETIME()),
                    source_file NVARCHAR(260) NULL,
                    {column_definitions}
                );
                CREATE INDEX IX_contencion_itau_vencida_fecha_carga ON {TABLE}(fecha_carga);
                CREATE INDEX IX_contencion_itau_vencida_source_file ON {TABLE}(source_file);
            END
            """
        )
        cn.commit()

        schema, table_name = TABLE.split(".")
        cur.execute(
            """
            SELECT c.name
            FROM sys.columns c
            INNER JOIN sys.tables t ON c.object_id = t.object_id
            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
            WHERE s.name = ? AND t.name = ?
            """,
            (schema, table_name),
        )
        existing = {row[0] for row in cur.fetchall()}

        for name, kind in COLUMN_SPECS:
            if name not in existing:
                cur.execute(f"ALTER TABLE {TABLE} ADD {sql_ident(name)} {sql_type_for(kind)};")
        cn.commit()


def insert_append(
    df: pd.DataFrame,
    source_file: str,
    fecha_carga: date,
    check_cancelled: Callable[[], None],
) -> int:
    insert_cols = ["fecha_carga", "source_file"] + EXPECTED_COLUMNS
    placeholders = ",".join(["?"] * len(insert_cols))
    sql = f"""
    INSERT INTO {TABLE} ({','.join(map(sql_ident, insert_cols))})
    VALUES ({placeholders})
    """

    rows = []
    for index, row in enumerate(df.itertuples(index=False, name=None), start=1):
        if index == 1 or index % BATCH_SIZE == 0:
            check_cancelled()
        out = [fecha_carga, source_file]
        for column, value in zip(EXPECTED_COLUMNS, row):
            out.append(value_for_insert(column, value))
        rows.append(tuple(out))

    inserted = 0
    check_cancelled()
    with connect() as cn:
        cn.autocommit = False
        cur = cn.cursor()
        cur.fast_executemany = True

        try:
            for i in range(0, len(rows), BATCH_SIZE):
                check_cancelled()
                batch = rows[i : i + BATCH_SIZE]
                cur.executemany(sql, batch)
                inserted += len(batch)
            check_cancelled()
            cn.commit()
        except Exception:
            cn.rollback()
            raise

    return inserted


def procesar_vencida(
    ruta_archivo: Path,
    fecha_carga: date,
    check_cancelled: Callable[[], None],
    source_file: str,
) -> int:
    check_cancelled()
    df = read_excel(ruta_archivo)
    check_cancelled()
    ensure_table()
    check_cancelled()
    return insert_append(df, source_file, fecha_carga, check_cancelled)


def validar_estructura_vencida(ruta_archivo: Path) -> int:
    return len(read_excel(ruta_archivo))
