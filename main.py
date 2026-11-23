from __future__ import annotations

from datetime import date
from pathlib import Path
from shutil import copyfileobj
from threading import Event, Lock
from typing import Callable, Literal
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from etl import castigo, vencida
from etl.castigo import procesar_castigo, validar_estructura_castigo
from etl.vencida import fecha_desde_nombre, procesar_vencida, validar_estructura_vencida

app = FastAPI(title="ETL Loader Itaú")

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

MODULOS_ETL = {"castigo": castigo, "vencida": vencida}
LIMITE_HISTORIAL = 20

active_jobs: dict[str, Event] = {}
active_jobs_lock = Lock()


class CargaCancelada(Exception):
    pass


@app.get("/")
def inicio() -> FileResponse:
    return FileResponse(BASE_DIR / "index.html")


def validar_excel(archivo: UploadFile) -> None:
    extension = Path(archivo.filename or "").suffix.lower()

    if extension != ".xlsx":
        raise HTTPException(
            status_code=400,
            detail="Solo se permiten archivos Excel .xlsx.",
        )


def ejecutar_etl(
    modulo: Literal["castigo", "vencida"],
    ruta_archivo: Path,
    fecha_carga: date,
    check_cancelled: Callable[[], None],
    source_file: str,
) -> int:
    if modulo == "castigo":
        return procesar_castigo(ruta_archivo, fecha_carga, check_cancelled, source_file)

    return procesar_vencida(ruta_archivo, fecha_carga, check_cancelled, source_file)


def validar_estructura(
    modulo: Literal["castigo", "vencida"],
    ruta_archivo: Path,
) -> int:
    if modulo == "castigo":
        return validar_estructura_castigo(ruta_archivo)

    return validar_estructura_vencida(ruta_archivo)


def consultar_historial(modulo: Literal["castigo", "vencida"]) -> list[dict]:
    """Cargas presentes en la tabla del modulo: una fila por fecha de carga y archivo."""
    etl = MODULOS_ETL[modulo]

    with etl.connect() as cn:
        cur = cn.cursor()
        if cur.execute("SELECT OBJECT_ID(?, 'U')", etl.TABLE).fetchone()[0] is None:
            return []

        cur.execute(
            f"""
            SELECT TOP (?) fecha_carga, source_file, COUNT(*) AS registros, MAX(ts_carga) AS ts_carga
            FROM {etl.TABLE}
            GROUP BY fecha_carga, source_file
            ORDER BY fecha_carga DESC, MAX(ts_carga) DESC
            """,
            LIMITE_HISTORIAL,
        )
        filas = cur.fetchall()

    return [
        {
            "archivo": fila.source_file,
            "fecha_carga": fila.fecha_carga.isoformat(),
            "fecha_proceso": fila.ts_carga.isoformat(timespec="seconds") if fila.ts_carga else None,
            "estado": "EXITOSA",
            "registros": fila.registros,
        }
        for fila in filas
    ]


@app.post("/api/validar/{modulo}")
def validar_archivo_previo(
    modulo: Literal["castigo", "vencida"],
    archivo: UploadFile = File(...),
) -> dict:
    validar_excel(archivo)

    nombre_seguro = f"validacion_{uuid4().hex}_{Path(archivo.filename or 'archivo.xlsx').name}"
    ruta_destino = UPLOAD_DIR / nombre_seguro

    try:
        with ruta_destino.open("wb") as destino:
            copyfileobj(archivo.file, destino)

        filas = validar_estructura(modulo, ruta_destino)

        return {
            "ok": True,
            "message": "Archivo valido para la carga seleccionada.",
            "filas": filas,
        }
    except HTTPException:
        raise
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"No fue posible validar el archivo: {error}",
        ) from error
    finally:
        archivo.file.close()
        ruta_destino.unlink(missing_ok=True)


@app.post("/api/cargas/{modulo}")
def procesar_archivo(
    modulo: Literal["castigo", "vencida"],
    fecha_carga: date = Form(...),
    job_id: str | None = Form(None),
    archivo: UploadFile = File(...),
) -> dict:
    validar_excel(archivo)

    job_id = job_id or uuid4().hex
    cancel_event = Event()
    with active_jobs_lock:
        active_jobs[job_id] = cancel_event

    def check_cancelled() -> None:
        if cancel_event.is_set():
            raise CargaCancelada("Carga cancelada por el usuario.")

    nombre_seguro = f"{uuid4().hex}_{Path(archivo.filename or 'archivo.xlsx').name}"
    nombre_original = Path(archivo.filename or "archivo.xlsx").name
    ruta_destino = UPLOAD_DIR / nombre_seguro
    if modulo == "vencida":
        # En Vencida manda la fecha del nombre del archivo (Contenciones ddmmyy).
        fecha_carga = fecha_desde_nombre(nombre_original) or fecha_carga

    try:
        check_cancelled()
        with ruta_destino.open("wb") as destino:
            copyfileobj(archivo.file, destino)

        check_cancelled()
        registros = ejecutar_etl(
            modulo=modulo,
            ruta_archivo=ruta_destino,
            fecha_carga=fecha_carga,
            check_cancelled=check_cancelled,
            source_file=nombre_original,
        )

        return {
            "ok": True,
            "message": "Archivo procesado correctamente.",
            "registros": registros,
        }

    except HTTPException:
        raise
    except CargaCancelada as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Error ejecutando el ETL: {error}",
        ) from error
    finally:
        archivo.file.close()
        with active_jobs_lock:
            active_jobs.pop(job_id, None)

        # Decide si conservarás o borrarás el archivo luego del ETL.
        # ruta_destino.unlink(missing_ok=True)


@app.post("/api/cargas/cancelar/{job_id}")
def cancelar_carga(job_id: str) -> dict:
    with active_jobs_lock:
        cancel_event = active_jobs.get(job_id)

    if cancel_event is None:
        return {"ok": False, "message": "La carga ya finalizo o no existe."}

    cancel_event.set()
    return {"ok": True, "message": "Cancelacion solicitada."}


@app.get("/api/cargas")
def listar_cargas(modulo: Literal["castigo", "vencida"]) -> list[dict]:
    try:
        return consultar_historial(modulo)
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"No fue posible leer el historial desde la base de datos: {error}",
        ) from error
