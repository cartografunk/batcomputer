# Dominio propuesto para Batcomputer

`batcomputer.cartografunk.com` es la dirección propuesta del servicio Railway que alojará frontend y API. Aún no existe una configuración de Railway ni registros DNS para ella. `www.cartografunk.com` seguirá servido por `cartografunk/cartografunk_web`; este procedimiento no cambia ese sitio.

## Cuando se autorice el despliegue

1. Despliegue `cartografunk/batcomputer` como servicio Railway independiente, con `DATABASE_URL` de PostgreSQL, variables privadas y healthcheck `/health` conforme al [README](../README.md). Compruebe primero el dominio temporal del servicio.
2. En el servicio Railway, abra **Settings → Networking → Public Networking → + Custom Domain** e introduzca `batcomputer.cartografunk.com`. Seleccione el puerto donde escucha la aplicación si Railway lo solicita; la imagen escucha en `PORT`.
3. Railway mostrará los registros DNS necesarios. Copie **exactamente** el nombre y valor del CNAME y del TXT de verificación al proveedor que administra `cartografunk.com`. No use valores de ejemplo ni modifique registros de `www`.
4. Espere a que Railway marque el dominio como verificado y emita su certificado HTTPS. Pruebe `https://batcomputer.cartografunk.com/health`, la interfaz, `/api` y `/flappy` antes de enlazarlo desde la web principal.
5. Ajuste `CORS_ORIGINS=https://batcomputer.cartografunk.com` en el servicio si corresponde y vuelva a probar el chat. El frontend y la API comparten origen, por lo que no se necesita acceso cruzado desde `www` para un enlace normal.
6. Revise la rama de trabajo de `cartografunk/cartografunk_web` con el enlace visible **Batcomputer** hacia `https://batcomputer.cartografunk.com/` y promuévala a producción solo después de probar el dominio. La aplicación incluye un enlace de regreso a `https://www.cartografunk.com/`.

| Tipo de registro | Nombre y valor que debe introducirse |
|---|---|
| CNAME | Los que Railway muestre al añadir el dominio personalizado |
| TXT de verificación | Los que Railway muestre al añadir el dominio personalizado |

Railway exige ambos registros para verificar y enrutar el dominio y gestiona el certificado HTTPS al completarse la configuración. Consulte la [documentación oficial de dominios personalizados de Railway](https://docs.railway.com/networking/domains/working-with-domains). Si el proveedor DNS usa un proxy, revise también en esa documentación las condiciones específicas antes de activarlo.

No se ha registrado este dominio en Railway, cambiado DNS, publicado el enlace de la web principal ni publicado un despliegue.
