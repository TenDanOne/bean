# Cambios de UI: mockup HTML primero

Regla de flujo para **cualquier cambio visual** de BEAN (templates/, CSS en theme/static_src/, static/marca/).
No aplica a cambios sin efecto visual (lógica, tests, traducción literal de textos ya aprobados).

## Flujo obligatorio

1. Antes de tocar templates o CSS, entrega un **mockup HTML autocontenido** en `mockups/mockup-<tema>.html`
   (raíz del repo, ignorado por git). Debe reproducir el look propuesto con los tokens reales de
   `theme/static_src/src/styles.css` (:root), Figtree y el isotipo de `static/marca/`, con los estados relevantes
   (vacío/lleno, hover, escritorio vs móvil) y el antes/después si ayuda a comparar.
2. El usuario lo abre en el navegador (doble clic al archivo). Si un lote toca varias pantallas, un solo mockup combinado.
3. Revisión visual con el agente `design-system-bean` (contraste AA, tokens, isotipo) antes de mostrarlo.
4. **Espera el sí/no o los cambios** del usuario. Itera el mockup hasta que lo apruebe.
5. Solo entonces edita los templates/CSS reales para que coincidan con el mockup aprobado; juez-bean al cerrar.
6. Borra el mockup cuando se implemente.

Mockup autocontenido: CSS/JS inline, fuentes e imágenes por ruta relativa al repo (`../static/...`), sin CDNs.
