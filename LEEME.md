# Bot de eventos de Aion 2

## 1. Crear el bot en Discord
1. Ve a https://discord.com/developers/applications → **New Application**.
2. Pestaña **Bot** → **Reset Token** y copia el token (no lo compartas; si se filtra, regéneralo).
3. No necesitas activar ningún "Privileged Intent".
4. Invita el bot con este enlace (cambia `TU_CLIENT_ID` por el *Application ID* de la pestaña General Information):

```
https://discord.com/oauth2/authorize?client_id=TU_CLIENT_ID&scope=bot%20applications.commands&permissions=268454912
```

Permisos incluidos: Gestionar roles, Ver canales, Enviar mensajes e Insertar enlaces.

## 2. Subir al hosting
1. Sube `bot.py`, `requirements.txt` y `.env` (copia `.env.example` y pon tu token).
2. Archivo de inicio: `bot.py`. Usa Python 3.11 o superior si el panel te deja elegir.
3. Asegúrate de que el panel instale `requirements.txt`.
4. Inicia el bot. La base de datos `aion2_eventos.db` se crea sola; no la borres.

## 3. Importante: jerarquía de roles
En *Ajustes del servidor → Roles*, el rol del bot debe tener **Gestionar roles**. Los roles de eventos que crea el bot
quedan por debajo del suyo, así que podrá asignarlos sin problema.

## 4. Comandos

### Admins (permiso "Gestionar servidor")
| Comando | Qué hace |
|---|---|
| `/evento crear` | Crea el evento, su rol y el primer aviso |
| `/evento rotacion` | Reajusta la hora de inicio / rotación / aviso previo |
| `/evento editar` | Cambia canal, descripción, imagen, emoji o color |
| `/evento probar` | Publica un embed de prueba sin etiquetar a nadie |
| `/evento borrar` | Elimina el evento (y opcionalmente su rol) |

### Todos los miembros
| Comando | Qué hace |
|---|---|
| `/suscribirse evento` | Te da el rol del evento |
| `/desuscribirse evento` | Te quita el rol |
| `/eventos` | Lista de eventos y cuándo son |

## 5. Formatos de hora (`inicio`)
- `20:30` → hoy a las 20:30 (hora de Colombia). Si ya pasó y el evento rota, salta al siguiente ciclo.
- `en 45` → dentro de 45 minutos.
- `25/10 21:00` o `2026-10-25 21:00` → fecha exacta.

## 6. Ejemplo
```
/evento crear nombre:Jefe de Mundo canal:#avisos inicio:20:00 cada_minutos:120 avisar_minutos:10 descripcion:Aparece en el mapa del norte
```
Cuando el jefe aparezca en otro momento, reajusta con:
```
/evento rotacion nombre:Jefe de Mundo inicio:en 120
```

El aviso se publica 10 minutos antes, etiqueta el rol del evento y, al llegar la hora, el mismo mensaje se edita a
"¡ha comenzado!". Después el bot programa solo el siguiente ciclo.
