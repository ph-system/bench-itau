# ETL Loader Itaú

Mockup funcional para cargar archivos Excel de Itaú Castigo e Itaú Vencida.

## Instalación

```powershell
py -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Ejecución

```powershell
uvicorn main:app --reload
```

Abrir:

```text
http://127.0.0.1:8000
```

## Integración con tus ETL

Edita la función `ejecutar_etl()` en `main.py`.

Ejemplo:

```python
from etl_castigo import procesar_castigo
from etl_vencida import procesar_vencida

if modulo == "castigo":
    return procesar_castigo(ruta_archivo, fecha_carga)

return procesar_vencida(ruta_archivo, fecha_carga)
```

Idealmente, cada ETL debe exponer una función y no depender únicamente de ejecutarse como script.

## Estructura

```text
itau_etl_loader/
├── index.html
├── main.py
├── requirements.txt
├── README.md
├── static/
│   ├── app.js
│   └── styles.css
└── uploads/
```
