# Mejoras futuras

Resumen de las mejoras planeadas que **todavía no están implementadas**. Cada mejora tiene su propio `.md` en esta carpeta con el plan completo; este documento solo las resume.

## Cómo agregar una mejora

1. Crear `documentacion/mejoras-futuras/<nombre-en-kebab-case>.md` con el plan: qué resuelve, estado actual, diseño por etapas, fuera de alcance y verificación.
2. Agregar una fila a la tabla de abajo y una sección corta de resumen.
3. Cuando se implemente, cambiar el estado aquí y en su `.md`, y mover lo que aplique a la documentación operativa.

## Tabla

| Mejora | Fase | Estado | Dificultad | Plan |
|---|---|---|---|---|
| OCR bajo demanda después del mapeo determinístico | 1 (dev) | Pendiente | Media | [`LATENCIA_RESPUESTA.md`](../LATENCIA_RESPUESTA.md) |
| Ubicación de cada campo en el documento y zonas de extracción | 2 (dev) | Pendiente | Media | [`ubicacion-campos-y-zonas.md`](ubicacion-campos-y-zonas.md) |

## Resúmenes

### OCR bajo demanda después del mapeo determinístico

- **Qué resuelve**: el pipeline manda hoy a Textract todas las páginas sin texto antes de comprobar si el perfil determinístico ya obtuvo todo lo necesario.
- **Evidencia**: en `emp_cert.pdf`, la segunda página solo contiene un logotipo y agregó entre 1.97 y 3.25 segundos de `pageLoad` más USD 0.0015, sin producir campos.
- **Enfoque**: texto nativo → preflight determinístico → Textract solo si faltan campos → IA compacta solo para lo todavía faltante.
- **Seguridad**: opt-in por catálogo y fail-open al comportamiento actual cuando el perfil no coincide o existe cualquier incertidumbre.
- **Meta del fixture**: cero Textract, cero IA y mediana de `processingSeconds <= 0.5 s`; no promete 0.5 segundos desde el upload.

### Ubicación de cada campo en el documento y zonas de extracción

- **Qué resuelve**: hoy el resultado dice qué valor se extrajo pero no dónde estaba. Se quiere devolver página + caja por campo para que el ERP/portal lo subraye, y poder declarar en el catálogo la zona donde vive un campo.
- **Enfoque**: conservar las cajas de palabras que Textract ya devuelve (y obtenerlas con pypdfium2 en PDFs nativos), y después de extraer buscar el valor crudo en esas palabras. No se le piden coordenadas al modelo porque sus cajas ya se midieron como poco fiables.
- **Zonas**: `hint` solo desempata entre ocurrencias; `fixed` lee de la zona y, si el dato no está ahí, lo busca en todo el documento y lo marca como reubicado.
- **Entrega**: `location` por campo en el API y en el SDK, activable por tipo desde el catálogo. Sin visor ni editor.
- **Fuera de alcance en la primera versión**: tablas y partidas de pedimento, campos derivados o calculados, flujo legado.
