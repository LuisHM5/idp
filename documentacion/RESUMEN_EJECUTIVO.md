<!--
  MANTENIMIENTO: Este diagrama es para audiencia directiva/negocio —
  a propósito NO menciona servicios, proveedores ni nombres técnicos.
  Si el flujo de negocio cambia (por ejemplo, se agrega un paso de
  aprobación humana), edita diagrams/resumen-ejecutivo.mmd y regenera
  (el .css agrega la sombra a las cajas, no es cosmético opcional):

    npx @mermaid-js/mermaid-cli -i documentacion/diagrams/resumen-ejecutivo.mmd \
      -o documentacion/diagrams/resumen-ejecutivo.png -b white -s 3 -w 1700 -H 700 \
      -C documentacion/diagrams/resumen-ejecutivo.css

  Para el detalle técnico (servicios, arquitectura, código) ver
  ARQUITECTURA.md — este documento es la versión sin tecnicismos.
-->

# Cómo funciona, en una imagen

Este documento explica el proceso **sin nombres de tecnología**, pensado para presentar a dirección o a un cliente que no necesita el detalle técnico. Si necesitas la arquitectura completa (servicios, diagramas técnicos), ver [`ARQUITECTURA.md`](ARQUITECTURA.md).

---

![Diagrama: llega un documento como una factura o pedimento, el sistema lo lee y entiende solo sin captura manual, se pregunta si la información es correcta y completa, si sí queda validado y listo para usarse, si no se separa para revisión con el motivo señalado, y en ambos casos el resultado queda disponible para quien lo necesite.](diagrams/resumen-ejecutivo.png)

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

_Fuente editable del diagrama: [`diagrams/resumen-ejecutivo.mmd`](diagrams/resumen-ejecutivo.mmd)._
