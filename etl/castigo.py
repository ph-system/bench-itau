from __future__ import annotations

import os
import re
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Callable

import msoffcrypto
import pandas as pd
import pyodbc
from msoffcrypto.exceptions import DecryptionError, FileFormatError, InvalidKeyError
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
EXCEL_PASSWORD = os.getenv("PASS_BENCH_ITAU_CASTIGO")

SHEET_NAME = 0
TABLE = "dbo.recup_itau_castigo"
BATCH_SIZE = 5000

COLUMN_SPECS = [
    ("CANAL_ASIG", "text"),
    ("ANO_DEL_CASTIGO", "integer"),
    ("CONVENIO", "text"),
    ("CONDICION_CONVENIO", "text"),
    ("RECUPERO", "integer"),
    ("FLAG_REC", "integer"),
    ("MONTO_CASTIGADO", "integer"),
    ("ORIGEN", "text"),
    ("ACRE", "text"),
    ("RUT", "integer"),
    ("DV", "text"),
    ("NOMBRES", "text"),
    ("APELLIDO_PAT", "text"),
    ("SUPERVISOR", "text"),
    ("COBRADOR", "text"),
    ("COBRADOR_DES", "text"),
    ("COBRADOR_AJUSTADO", "text"),
    ("META", "decimal4"),
    ("CALCULO_META", "integer"),
    ("RESPONSABLE", "text"),
    ("FECHA_RECUPERO", "text_date"),
    ("GESTOR", "text"),
    ("SEGMENTO", "text"),
    ("SEG_AGRUP", "text"),
    ("ZONA", "text"),
    ("ZONAL", "text"),
    ("DESGLOSE_NO_ASIGNABLE", "text"),
    ("ESTADO_JUDICIAL", "text"),
    ("EJECUTIVO_JUDICIAL", "text"),
    ("ABOGADO", "text"),
    ("ROL", "text"),
    ("TRIBUNAL", "text"),
    ("SEG", "text"),
    ("RECMAYOR$MM100", "text"),
    ("CLUSTER", "text"),
    ("TIPO", "text"),
    ("MARCA", "text"),
    ("MARCA_CJ", "text"),
    ("TIPO_CARTERA_JUDICIAL", "text"),
    ("CON_HIPO", "text"),
    ("BIENES_RAICES", "text"),
    ("BNS", "text"),
    ("REGION", "text"),
    ("TIPO_GESTION", "text"),
    ("TIPO_PERSONA", "text"),
    ("GESTIONADO", "integer"),
    ("FECHA_COMPROMISO", "integer"),
    ("DIAS_COMPROMISO", "integer"),
    ("MONTO_COMPROMISO", "integer"),
    ("COMPROMISO_VIGENTE", "text"),
    ("DP_CIERRE_TOT", "integer"),
    ("DP_CIERRE_ACT", "integer"),
    ("PROY_TUBO", "integer"),
    ("FALTANTE", "integer"),
    ("VEL_NEC", "integer"),
    ("VEL_ACUM", "integer"),
    ("BRECHA", "integer"),
    ("MOB", "text"),
    ("MOB_AGRUP", "text"),
    ("NOTA", "text"),
    ("NOTA_CHECK_PILOTO", "text"),
    ("SEG_PILOTO_NOTA", "text"),
    ("META_PILOTO_NOTA", "decimal4"),
    ("CALCULO_META_PILOTO", "integer"),
    ("PROY_TUBO_PILOTO", "integer"),
    ("FALTANTE_PILOTO", "integer"),
    ("VEL_NEC_PILOTO", "integer"),
    ("VEL_ACUM_PILOTO", "integer"),
    ("BRECHA_PILOTO", "integer"),
    ("FECHA_PROCESO", "date_ddmmyyyy"),
    ("NT", "integer"),
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
    text = str(value).replace("\x00", "").replace("\ufeff", "")
    text = re.sub(r"[\u0000-\u001f\u007f]+", "", text)
    return re.sub(r"\s+", "_", text.strip()).upper()


def clean_cell(value: object) -> str | None:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text.replace("\x00", "")


def parse_decimal(value: object, scale: int) -> str | None:
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
        number = Decimal(text)
    except Exception:
        return None

    if scale == 0:
        return str(int(number))
    return str(number.quantize(Decimal("1." + "0" * scale)))


def parse_fecha_proceso(value: object):
    if value is None or value is pd.NA:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, pd.Timestamp):
        return value.date()

    text = clean_cell(value)
    if text is None:
        return None

    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def sql_type_for(kind: str) -> str:
    if kind == "integer":
        return "DECIMAL(38,0) NULL"
    if kind == "decimal4":
        return "DECIMAL(38,4) NULL"
    if kind == "date_ddmmyyyy":
        return "DATE NULL"
    if kind == "text_date":
        return "NVARCHAR(50) NULL"
    return "NVARCHAR(100) NULL"


def read_excel(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"No existe el Excel en: {path}")
    if not EXCEL_PASSWORD:
        raise ValueError(
            "No se puede procesar Itau Castigo porque falta la clave del archivo "
            "PASS_BENCH_ITAU_CASTIGO en .env."
        )

    decrypted = BytesIO()
    try:
        with path.open("rb") as handle:
            office_file = msoffcrypto.OfficeFile(handle)
            office_file.load_key(password=EXCEL_PASSWORD)
            office_file.decrypt(decrypted)
    except (DecryptionError, InvalidKeyError):
        raise ValueError(
            "No fue posible abrir el Excel de Itau Castigo. "
            "Revisa que la clave configurada sea correcta."
        ) from None
    except FileFormatError:
        raise ValueError(
            "El archivo no es un Excel protegido valido para Itau Castigo. "
            "Revisa que corresponda al formato esperado."
        ) from None
    except Exception as error:
        raise ValueError(
            "No fue posible leer el Excel de Itau Castigo. "
            f"Detalle: {error}"
        ) from error

    decrypted.seek(0)
    try:
        df = pd.read_excel(decrypted, sheet_name=SHEET_NAME, dtype=object, engine="openpyxl")
    except Exception as error:
        raise ValueError(
            "El archivo no es un Excel .xlsx valido para Itau Castigo o esta danado. "
            f"Detalle: {error}"
        ) from error
    df.columns = [normalize_excel_col(c) for c in df.columns]
    df = df.where(pd.notnull(df), None)

    missing = [col for col in EXPECTED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(
            "El archivo no tiene la estructura esperada para Itau Castigo. "
            "Faltan columnas: " + ", ".join(missing)
        )

    return df[EXPECTED_COLUMNS]


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
                    id_recup_itau_castigo BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
                    fecha_carga DATE NOT NULL CONSTRAINT DF_recup_itau_castigo_fecha_carga DEFAULT (CONVERT(date, GETDATE())),
                    ts_carga DATETIME2(0) NOT NULL CONSTRAINT DF_recup_itau_castigo_ts_carga DEFAULT (SYSDATETIME()),
                    source_file NVARCHAR(260) NULL,
                    {column_definitions}
                );
                CREATE INDEX IX_recup_itau_castigo_fecha_carga ON {TABLE}(fecha_carga);
            END
            """
        )
        cn.commit()


def value_for_insert(column: str, value: object):
    kind = COLUMN_KINDS[column]
    if kind == "integer":
        return parse_decimal(value, 0)
    if kind == "decimal4":
        return parse_decimal(value, 4)
    if kind == "date_ddmmyyyy":
        return parse_fecha_proceso(value)
    return clean_cell(value)


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


def procesar_castigo(
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


def validar_estructura_castigo(ruta_archivo: Path) -> int:
    return len(read_excel(ruta_archivo))
