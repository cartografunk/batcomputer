# Dominio de Batcomputer

La aplicación completa (frontend y API) se sirve desde Railway en `https://batcomputer.cartografunk.com/`. El sitio principal `cartografunk.com` permanece en el repositorio independiente `cartografunk/cartografunk_web` y Cloudflare Pages.

## Configuración aplicada

- Servicio Railway `batcomputer`, puerto `8000`, una réplica y healthcheck `/health`.
- Dominio personalizado `batcomputer.cartografunk.com`, añadido en **Settings → Networking → Public Networking**.
- Cloudflare añadió los registros CNAME `batcomputer` y TXT `_railway-verify.batcomputer` con los valores entregados por Railway. Consulte el panel de Railway antes de recrearlos: esos valores pueden cambiar si se elimina y vuelve a crear el dominio.
- El CNAME está bajo el proxy de Cloudflare. Railway detecta el proxy; el TXT queda en modo DNS only. La [documentación de Railway](https://docs.railway.com/networking/domains/working-with-domains) indica usar el modo SSL/TLS **Full** al mantener ese proxy.
- Se comprobó `https://batcomputer.cartografunk.com/health` con HTTP 200 y `{"status":"ok"}` el 8 de octubre de 2026.

El enlace **Batcomputer** de la portada de Cartografunk apunta al subdominio. La aplicación incluye un enlace de regreso a `https://www.cartografunk.com/`.

## Verificación tras cambios

1. Compruebe que Railway indique el despliegue como activo y que `/health` responda 200.
2. Pruebe la interfaz, un ticket real y `/flappy` en el subdominio. El healthcheck comprueba la conexión SQL, pero no una llamada al proveedor de IA.
3. Si cambia el dominio, actualice `CORS_ORIGINS` en Railway y el enlace del sitio principal. El frontend y la API comparten origen; el enlace desde Cartografunk no requiere CORS.

Publicar bajo `www.cartografunk.com/agentes` necesitaría un proxy de rutas en el hosting del sitio principal; no forma parte de esta configuración.
