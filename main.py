# main.py
from fastapi.responses import FileResponse
from datetime import date, timedelta
from typing import Literal, Optional
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from codigos import CODIGOS_ANEXO_51_32, agregar_estado

app = FastAPI(
    title="API Panel Aduana Chile",
    description="Indicadores (UF, Dólar), conversores aduaneros, calculadora CIF y códigos Anexo 51-32.",
    version="1.0.0",
)

# Permitir peticiones desde tu index.html local
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", include_in_schema=False)
def servir_panel():
    return FileResponse("index.html")
# =========================================================
# 1. INDICADORES (UF y Dólar Observado)
# =========================================================

async def _fetch_uf_fecha(fecha: date) -> Optional[dict]:
    fecha_str = fecha.strftime("%d-%m-%Y")
    fuentes = [
        ("mindicador.cl", f"https://mindicador.cl/api/uf/{fecha_str}"),
        ("findic.cl", f"https://findic.cl/api/uf/{fecha_str}"),
    ]
    async with httpx.AsyncClient(timeout=4.0) as client:
        for nombre, url in fuentes:
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    data = r.json()
                    serie = data.get("serie", [])
                    if serie and serie[0].get("valor"):
                        return {
                            "valor": float(serie[0]["valor"]),
                            "fecha_dato": str(serie[0].get("fecha", ""))[:10] or fecha.isoformat(),
                            "fuente": nombre,
                        }
            except Exception:
                continue
    return None


@app.get("/indicadores/uf", tags=["Indicadores"])
async def obtener_uf(fecha: Optional[date] = Query(default=None, description="Formato YYYY-MM-DD")):
    """Obtiene la UF para una fecha (o hoy), con respaldo en findic.cl y retroceso de hasta 3 días."""
    fecha_consulta = fecha or date.today()

    for retroceso in range(0, 4):  # Día consultado + hasta 3 días atrás
        dia = fecha_consulta - timedelta(days=retroceso)
        resultado = await _fetch_uf_fecha(dia)
        if resultado:
            return {"fecha_solicitada": fecha_consulta.isoformat(), **resultado}

    raise HTTPException(status_code=503, detail="No se pudo obtener la UF desde mindicador.cl ni findic.cl")


@app.get("/indicadores/dolar-observado", tags=["Indicadores"])
async def obtener_dolar_observado():
    """Obtiene el Dólar Observado actual."""
    urls = [
        ("mindicador.cl", "https://mindicador.cl/api/dolar"),
        ("findic.cl", "https://findic.cl/api/dolar"),
    ]
    async with httpx.AsyncClient(timeout=4.0) as client:
        for nombre, url in urls:
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    serie = r.json().get("serie", [])
                    if serie and serie[0].get("valor"):
                        return {"valor": float(serie[0]["valor"]), "fuente": nombre}
            except Exception:
                continue
    raise HTTPException(status_code=503, detail="No se pudo obtener el Dólar Observado")


# =========================================================
# 2. CONVERSORES (Dólar Aduanero, Acuerdo y UF)
# =========================================================

class ConversionRequest(BaseModel):
    tipo: Literal["dolar_aduana", "dolar_acuerdo", "uf"]
    monto: float = Field(..., gt=0, description="Monto a convertir")
    tasa: float = Field(..., gt=0, description="Valor en CLP del dólar o UF")
    inverso: bool = Field(default=False, description="False: Origen->CLP | True: CLP->Origen")
    uf_referencia: Optional[float] = Field(default=None, gt=0, description="Opcional para calcular equivalente en UF")


@app.post("/calcular/conversion", tags=["Calculadoras"])
def calcular_conversion(req: ConversionRequest):
    if not req.inverso:
        total_clp = req.monto * req.tasa
        res = {"modo": "directo", "total_clp": round(total_clp, 2)}
        if req.uf_referencia and req.tipo != "uf":
            res["equivalente_uf"] = round(total_clp / req.uf_referencia, 4)
        return res
    else:
        resultado = req.monto / req.tasa
        unidad = "uf" if req.tipo == "uf" else "usd"
        res = {"modo": "inverso", f"equivalente_{unidad}": round(resultado, 4 if unidad == "uf" else 2)}
        if req.uf_referencia and req.tipo != "uf":
            res["equivalente_uf"] = round(req.monto / req.uf_referencia, 4)
        return res


# =========================================================
# 3. CALCULADORA CIF (Directa e Inversa)
# =========================================================

class CifRequest(BaseModel):
    valor: float = Field(..., gt=0, description="Valor CIF (USD) en modo directo, o Presupuesto (CLP) en inverso")
    porcentaje: float = Field(..., gt=0, description="Ej: 6 para 6% Ad-valorem, o 0.11 para 0.11%")
    tipo_cambio: float = Field(..., gt=0, description="Dólar Aduanero en CLP")
    inverso: bool = Field(default=False, description="False: CIF->CLP | True: CLP->CIF máximo")


@app.post("/calcular/cif", tags=["Calculadoras"])
def calcular_cif(req: CifRequest):
    factor = req.porcentaje / 100.0
    if not req.inverso:
        subtotal_usd = req.valor * factor
        total_clp = subtotal_usd * req.tipo_cambio
        return {
            "modo": "directo",
            "valor_cif_usd": round(req.valor, 2),
            "porcentaje": req.porcentaje,
            "subtotal_usd": round(subtotal_usd, 6),
            "tipo_cambio_clp": round(req.tipo_cambio, 2),
            "total_pagar_clp": round(total_clp, 2),
        }
    else:
        subtotal_usd = req.valor / req.tipo_cambio
        cif_max_usd = subtotal_usd / factor
        return {
            "modo": "inverso",
            "presupuesto_clp": round(req.valor, 2),
            "tipo_cambio_clp": round(req.tipo_cambio, 2),
            "subtotal_usd": round(subtotal_usd, 6),
            "porcentaje": req.porcentaje,
            "valor_cif_maximo_usd": round(cif_max_usd, 2),
        }


# =========================================================
# 4. CÓDIGOS DE GIRO ADUANERO (Anexo 51-32)
# =========================================================

@app.get("/codigos", tags=["Códigos Aduaneros"])
def buscar_codigos(q: Optional[str] = Query(default=None, description="Buscar por código, descripción o fuente")):
    if not q:
        return [agregar_estado(c) for c in CODIGOS_ANEXO_51_32]

    termino = q.strip().lower()
    termino_norm = termino.replace(".", "").replace(" ", "")

    resultados = [
        agregar_estado(c)
        for c in CODIGOS_ANEXO_51_32
        if termino in c["codigo"].lower()
        or termino_norm in c["codigo"].replace(".", "").lower()
        or termino in c["descripcion"].lower()
        or termino in c["fuente"].lower()
    ]
    return resultados


@app.get("/codigos/{codigo}", tags=["Códigos Aduaneros"])
def obtener_codigo(codigo: str):
    norm = codigo.replace(".", "").strip()
    for c in CODIGOS_ANEXO_51_32:
        if c["codigo"] == codigo or c["codigo"].replace(".", "") == norm:
            return agregar_estado(c)
    raise HTTPException(status_code=404, detail=f"Código {codigo} no encontrado")