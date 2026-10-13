# Cómo funciona, en simple

Este documento explica el proceso **sin nombres de tecnología**, pensado para presentar a dirección o a un cliente que no necesita el detalle técnico. Si necesitas la arquitectura completa (servicios, diagramas técnicos), ver [`ARQUITECTURA.md`](ARQUITECTURA.md).

---

## En palabras simples

1. **Llega un documento.** Alguien sube una factura, un pedimento aduanal u otro documento del negocio.
2. **El sistema lo lee solo.** No hay una persona capturando datos a mano — el sistema entiende el contenido del documento automáticamente, en minutos, las 24 horas.
3. **Revisa que todo esté correcto.** Compara la información contra las reglas del negocio (montos que cuadren, folios válidos, campos obligatorios presentes, etc.).
4. **Dos caminos posibles:**
   - ✅ **Todo en orden** → el documento queda validado y disponible para usarse de inmediato.
   - ⚠️ **Algo no cuadra** → se separa para que alguien lo revise, con el motivo exacto ya identificado (no hay que buscarlo).
5. **El resultado queda disponible.** En cualquiera de los dos casos, el resultado queda registrado y listo para consultarse — no hay que ir a buscarlo en papel ni pedirlo por separado.

## Qué gana el negocio con esto

- **Menos trabajo manual de captura** — el tiempo del equipo se enfoca en revisar solo los casos que realmente lo necesitan, no todos.
- **Errores detectados antes**, con el motivo exacto, en vez de descubrirlos después en un proceso posterior.
- **Disponible todo el tiempo**, no depende de horario de oficina ni de que una persona esté libre para capturar.
- **Trazabilidad**: cada documento procesado queda registrado, con quién/qué lo validó y cuándo.
