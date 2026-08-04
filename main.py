from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from shutil import copyfileobj
from threading import Event, Lock
from typing import Callable, Literal
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from etl.castigo import procesar_castigo, validar_estructura_castigo
from etl.vencida import procesar_vencida, validar_estructura_vencida

app = FastAPI(title="ETL Loader Itaú")

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# Ejemplo simple en memoria.
# En producción, reemplázalo por una tabla de auditoría en SQL Server.
historial: list[dict] = []
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


def registrar_historial(
    modulo: Literal["castigo", "vencida"],
    archivo: str | None,
    fecha_carga: date,
    estado: str,
    registros: int | None,
) -> None:
    historial.insert(
        0,
        {
            "modulo": "Itaú Castigo" if modulo == "castigo" else "Itaú Vencida",
            "archivo": archivo,
            "fecha_carga": fecha_carga.isoformat(),
            "fecha_proceso": datetime.now().isoformat(timespec="seconds"),
            "estado": estado,
            "registros": registros,
        },
    )


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

        registrar_historial(modulo, archivo.filename, fecha_carga, "EXITOSA", registros)

        return {
            "ok": True,
            "message": "Archivo procesado correctamente.",
            "registros": registros,
        }

    except HTTPException:
        raise
    except CargaCancelada as error:
        registrar_historial(modulo, archivo.filename, fecha_carga, "CANCELADA", None)
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        registrar_historial(modulo, archivo.filename, fecha_carga, "ERROR", None)
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        registrar_historial(modulo, archivo.filename, fecha_carga, "ERROR", None)

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
def listar_cargas() -> list[dict]:
    return historial[:20]
