"""
Bot de Discord para avisos de eventos de Aion 2
(jefes de mundo, grieta dimensional, etc.)

- Los admins crean/ajustan eventos con /evento ...
- Cualquier miembro usa /suscribirse y /desuscribirse (el bot le da/quita el rol)
- Antes de cada evento el bot publica un embed y etiqueta el rol del evento
"""
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TZ = ZoneInfo(os.getenv("BOT_TZ", "America/Bogota"))
DB_PATH = os.getenv("DB_PATH", "aion2_eventos.db")
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID")  # opcional: sincroniza comandos al instante en un servidor

DEFAULT_COLOR = 0x5865F2
GREEN = 0x57F287

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("aion2-bot")

TIME_HELP = (
    "Formato de hora no válido. Usa por ejemplo: `20:30` (hora de Colombia), "
    "`en 45` (dentro de 45 minutos), `25/10 21:00` o `2026-10-25 21:00`."
)


# --------------------------------------------------------------------------- #
# Base de datos
# --------------------------------------------------------------------------- #
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id     INTEGER NOT NULL,
                name         TEXT    NOT NULL COLLATE NOCASE,
                channel_id   INTEGER NOT NULL,
                role_id      INTEGER,
                emoji        TEXT    NOT NULL DEFAULT '⚔️',
                description  TEXT    NOT NULL DEFAULT '',
                image_url    TEXT,
                color        INTEGER NOT NULL DEFAULT 5793266,
                interval_min INTEGER NOT NULL DEFAULT 0,
                notify_min   INTEGER NOT NULL DEFAULT 10,
                next_start   INTEGER NOT NULL,
                notified_for INTEGER NOT NULL DEFAULT 0,
                message_id   INTEGER,
                UNIQUE (guild_id, name)
            )
            """
        )


def get_event(guild_id: int, name: str) -> Optional[dict]:
    with db() as c:
        row = c.execute(
            "SELECT * FROM events WHERE guild_id = ? AND name = ?", (guild_id, name.strip())
        ).fetchone()
    return dict(row) if row else None


def list_events(guild_id: int) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT * FROM events WHERE guild_id = ? ORDER BY next_start", (guild_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def update_event(event_id: int, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db() as c:
        c.execute(f"UPDATE events SET {cols} WHERE id = ?", (*fields.values(), event_id))


def delete_event(event_id: int):
    with db() as c:
        c.execute("DELETE FROM events WHERE id = ?", (event_id,))


# --------------------------------------------------------------------------- #
# Utilidades de tiempo / formato
# --------------------------------------------------------------------------- #
class TimeParseError(ValueError):
    pass


def parse_start(text: str) -> tuple[int, bool]:
    """Devuelve (epoch, solo_hora). 'solo_hora' = el usuario dio solo HH:MM."""
    t = text.strip().lower()
    try:
        m = re.fullmatch(r"(?:en\s+|\+)\s*(\d{1,5})\s*(?:m|min|minutos)?", t)
        if m:
            return int(time.time()) + int(m.group(1)) * 60, False

        m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
        if m:
            now = datetime.now(TZ)
            dt = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            return int(dt.timestamp()), True

        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", t)
        if m:
            d, mo, h, mi = map(int, m.groups())
            dt = datetime(datetime.now(TZ).year, mo, d, h, mi, tzinfo=TZ)
            return int(dt.timestamp()), False

        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})", t)
        if m:
            y, mo, d, h, mi = map(int, m.groups())
            dt = datetime(y, mo, d, h, mi, tzinfo=TZ)
            return int(dt.timestamp()), False
    except ValueError:
        pass
    raise TimeParseError(TIME_HELP)


def resolve_start(text: str, interval_min: int) -> int:
    """Convierte el texto del usuario en el PRÓXIMO inicio (siempre en el futuro).

    Si la hora dada ya pasó y el evento rota, salta al siguiente ciclo.
    Ej.: "20:00" con rotación de 120 min y ahora son las 21:00 -> 22:00.
    """
    start, clock_only = parse_start(text)
    now = int(time.time())
    if start > now:
        return start
    if interval_min > 0:
        step = interval_min * 60
        return start + ((now - start) // step + 1) * step
    if clock_only:
        return start + 86400
    raise TimeParseError("Esa fecha/hora ya pasó.")


def parse_color(text: str) -> Optional[int]:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", text.strip())
    return int(m.group(1), 16) if m else None


def fmt_minutes(total: int) -> str:
    h, m = divmod(total, 60)
    parts = []
    if h:
        parts.append(f"{h} h")
    if m:
        parts.append(f"{m} min")
    return " ".join(parts) or "0 min"


def valid_url(url: str) -> bool:
    return url.startswith(("http://", "https://"))


# --------------------------------------------------------------------------- #
# Embed
# --------------------------------------------------------------------------- #
def build_embed(ev: dict, *, started: bool = False, test: bool = False) -> discord.Embed:
    emoji = ev["emoji"] or "⚔️"
    start = ev["next_start"]
    prefix = "[PRUEBA] " if test else ""

    if started:
        title = f"{prefix}{emoji} ¡{ev['name']} ha comenzado!"
        lead = "🔥 **¡Ya está en curso!** Entra ahora al juego."
        color = GREEN
    else:
        title = f"{prefix}{emoji} {ev['name']}"
        lead = f"⏳ **¡Prepárate!** Empieza <t:{start}:R>."
        color = ev["color"]

    desc = lead
    if ev["description"]:
        desc += f"\n\n{ev['description']}"

    embed = discord.Embed(
        title=title,
        description=desc,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(name="Aion 2 · Aviso de evento")
    embed.add_field(name="🕒 Inicio", value=f"<t:{start}:F>\n(<t:{start}:R>)", inline=True)

    if ev["interval_min"] > 0:
        embed.add_field(name="🔁 Rotación", value=f"Cada {fmt_minutes(ev['interval_min'])}", inline=True)
        nxt = start + ev["interval_min"] * 60
        embed.add_field(name="⏭️ Siguiente", value=f"<t:{nxt}:t>", inline=True)
    else:
        embed.add_field(name="🔁 Rotación", value="Evento único", inline=True)

    if ev["image_url"]:
        embed.set_image(url=ev["image_url"])
    embed.set_footer(text="Usa /suscribirse para recibir estos avisos")
    return embed


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #
class AionBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        init_db()
        self.tree.add_command(evento_group)
        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        scheduler.start()

    async def on_ready(self):
        log.info("Conectado como %s (id %s)", self.user, self.user.id)


bot = AionBot()


async def get_channel(channel_id: int):
    ch = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden):
            return None
    return ch


async def ensure_role(guild: discord.Guild, ev: dict) -> discord.Role:
    """Devuelve el rol del evento; si lo borraron, lo vuelve a crear."""
    role = guild.get_role(ev["role_id"]) if ev["role_id"] else None
    if role is None:
        role = await guild.create_role(
            name=f"🔔 {ev['name']}"[:100],
            mentionable=True,
            reason=f"Rol de suscripción para el evento {ev['name']}",
        )
        update_event(ev["id"], role_id=role.id)
        ev["role_id"] = role.id
    return role


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #
async def process_event(ev: dict):
    now = int(time.time())
    start = ev["next_start"]
    notify_s = ev["notify_min"] * 60

    channel = await get_channel(ev["channel_id"])
    role_id = ev["role_id"]
    mention = f"<@&{role_id}>" if role_id else None
    allowed = (
        discord.AllowedMentions(roles=[discord.Object(id=role_id)])
        if role_id
        else discord.AllowedMentions.none()
    )

    # 1) Ya es hora: editar el aviso a "en curso" (o enviarlo si no había aviso previo)
    if now >= start:
        if channel is not None:
            if ev["notified_for"] == start and ev["message_id"]:
                try:
                    msg = await channel.fetch_message(ev["message_id"])
                    await msg.edit(embed=build_embed(ev, started=True))
                except discord.HTTPException:
                    pass
            elif ev["notified_for"] != start and now - start <= 120:
                try:
                    await channel.send(
                        content=mention, embed=build_embed(ev, started=True), allowed_mentions=allowed
                    )
                except discord.HTTPException as e:
                    log.warning("No pude enviar el aviso de inicio de %s: %s", ev["name"], e)
        else:
            log.warning("Canal no disponible para el evento %s", ev["name"])

        # Avanzar al siguiente ciclo (o borrar si era único)
        if ev["interval_min"] > 0:
            step = ev["interval_min"] * 60
            new_start = start + ((now - start) // step + 1) * step
            update_event(ev["id"], next_start=new_start, message_id=None)
        else:
            delete_event(ev["id"])
        return

    # 2) Ventana de aviso previo
    if notify_s > 0 and now >= start - notify_s and ev["notified_for"] != start:
        message_id = None
        if channel is not None:
            try:
                msg = await channel.send(
                    content=mention, embed=build_embed(ev), allowed_mentions=allowed
                )
                message_id = msg.id
            except discord.HTTPException as e:
                log.warning("No pude enviar el aviso de %s: %s", ev["name"], e)
        else:
            log.warning("Canal no disponible para el evento %s", ev["name"])
        # Se marca aunque falle, para no reintentar cada 15 s
        update_event(ev["id"], notified_for=start, message_id=message_id)


@tasks.loop(seconds=15)
async def scheduler():
    with db() as c:
        rows = c.execute("SELECT * FROM events").fetchall()
    for row in rows:
        try:
            await process_event(dict(row))
        except Exception:
            log.exception("Error procesando el evento %s", row["name"])


@scheduler.before_loop
async def _before_scheduler():
    await bot.wait_until_ready()


# --------------------------------------------------------------------------- #
# Autocompletado
# --------------------------------------------------------------------------- #
async def evento_autocomplete(interaction: discord.Interaction, current: str):
    if interaction.guild_id is None:
        return []
    cur = current.lower()
    return [
        app_commands.Choice(name=e["name"], value=e["name"])
        for e in list_events(interaction.guild_id)
        if cur in e["name"].lower()
    ][:25]


# --------------------------------------------------------------------------- #
# Comandos de administración: /evento ...
# --------------------------------------------------------------------------- #
evento_group = app_commands.Group(
    name="evento",
    description="Administra los eventos de Aion 2",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@evento_group.command(name="crear", description="Crea un evento con rotación, embed y rol de suscripción")
@app_commands.describe(
    nombre="Nombre del evento (ej. Jefe de Mundo)",
    canal="Canal donde se publicarán los avisos",
    inicio='Cuándo empieza: "20:30" (hora Colombia), "en 45", "25/10 21:00"',
    cada_minutos="Cada cuántos minutos se repite (0 = evento único)",
    avisar_minutos="Minutos de aviso antes del inicio (0 = solo al empezar)",
    descripcion="Texto extra para el embed (ubicación, recompensas...)",
    imagen="URL de una imagen para el embed (opcional)",
    emoji="Emoji del evento (opcional)",
    color="Color en hex, ej. #ff5500 (opcional)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def evento_crear(
    interaction: discord.Interaction,
    nombre: str,
    canal: discord.TextChannel,
    inicio: str,
    cada_minutos: app_commands.Range[int, 0, 43200] = 0,
    avisar_minutos: app_commands.Range[int, 0, 1440] = 10,
    descripcion: str = "",
    imagen: Optional[str] = None,
    emoji: str = "⚔️",
    color: Optional[str] = None,
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    nombre = nombre.strip()

    if not 2 <= len(nombre) <= 60:
        return await interaction.followup.send("❌ El nombre debe tener entre 2 y 60 caracteres.")
    if get_event(guild.id, nombre):
        return await interaction.followup.send(f"❌ Ya existe un evento llamado **{nombre}**.")

    try:
        start = resolve_start(inicio, cada_minutos)
    except TimeParseError as e:
        return await interaction.followup.send(f"❌ {e}")

    col = DEFAULT_COLOR
    if color:
        col = parse_color(color)
        if col is None:
            return await interaction.followup.send("❌ Color inválido. Usa formato hex, ej. `#ff5500`.")
    if imagen and not valid_url(imagen):
        return await interaction.followup.send("❌ La imagen debe ser una URL que empiece con http(s)://")

    perms = canal.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        return await interaction.followup.send(
            f"❌ No tengo permisos en {canal.mention}. Necesito: ver canal, enviar mensajes e insertar enlaces."
        )
    if not guild.me.guild_permissions.manage_roles:
        return await interaction.followup.send("❌ Me falta el permiso **Gestionar roles** para crear el rol del evento.")

    try:
        role = await guild.create_role(
            name=f"🔔 {nombre}"[:100],
            mentionable=True,
            reason=f"Rol de suscripción para el evento {nombre}",
        )
    except discord.Forbidden:
        return await interaction.followup.send("❌ No pude crear el rol (revisa mis permisos).")

    with db() as c:
        c.execute(
            """INSERT INTO events
               (guild_id, name, channel_id, role_id, emoji, description, image_url,
                color, interval_min, notify_min, next_start)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (guild.id, nombre, canal.id, role.id, emoji, descripcion[:500], imagen,
             col, cada_minutos, avisar_minutos, start),
        )
    ev = get_event(guild.id, nombre)
    await interaction.followup.send(
        f"✅ Evento **{nombre}** creado en {canal.mention}. Rol de avisos: {role.mention}\n"
        f"Primer inicio: <t:{start}:F> (<t:{start}:R>). Así se verá:",
        embed=build_embed(ev),
    )


@evento_group.command(name="rotacion", description="Reajusta cuándo empieza el evento y/o cada cuánto rota")
@app_commands.describe(
    nombre="Evento a ajustar",
    inicio='Hora de inicio: "20:30", "en 45", "25/10 21:00". Si ya pasó, salta al siguiente ciclo',
    cada_minutos="Nueva rotación en minutos (deja vacío para mantener la actual)",
    avisar_minutos="Nuevo aviso previo en minutos (deja vacío para mantener)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def evento_rotacion(
    interaction: discord.Interaction,
    nombre: str,
    inicio: str,
    cada_minutos: Optional[app_commands.Range[int, 0, 43200]] = None,
    avisar_minutos: Optional[app_commands.Range[int, 0, 1440]] = None,
):
    await interaction.response.defer(ephemeral=True)
    ev = get_event(interaction.guild_id, nombre)
    if not ev:
        return await interaction.followup.send(f"❌ No encontré el evento **{nombre}**.")

    interval = ev["interval_min"] if cada_minutos is None else cada_minutos
    notify = ev["notify_min"] if avisar_minutos is None else avisar_minutos
    try:
        start = resolve_start(inicio, interval)
    except TimeParseError as e:
        return await interaction.followup.send(f"❌ {e}")

    update_event(
        ev["id"], next_start=start, interval_min=interval, notify_min=notify,
        notified_for=0, message_id=None,
    )
    await interaction.followup.send(
        f"✅ **{ev['name']}** reajustado.\n"
        f"Próximo inicio: <t:{start}:F> (<t:{start}:R>) · "
        f"Rotación: {'cada ' + fmt_minutes(interval) if interval else 'evento único'} · "
        f"Aviso: {notify} min antes."
    )


@evento_group.command(name="editar", description="Edita el aspecto del evento (canal, texto, imagen, emoji, color)")
@app_commands.describe(
    nombre="Evento a editar",
    canal="Nuevo canal de avisos",
    descripcion='Nuevo texto del embed (escribe "-" para borrarlo)',
    imagen='Nueva URL de imagen (escribe "-" para quitarla)',
    emoji="Nuevo emoji",
    color="Nuevo color hex, ej. #ff5500",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def evento_editar(
    interaction: discord.Interaction,
    nombre: str,
    canal: Optional[discord.TextChannel] = None,
    descripcion: Optional[str] = None,
    imagen: Optional[str] = None,
    emoji: Optional[str] = None,
    color: Optional[str] = None,
):
    await interaction.response.defer(ephemeral=True)
    ev = get_event(interaction.guild_id, nombre)
    if not ev:
        return await interaction.followup.send(f"❌ No encontré el evento **{nombre}**.")

    changes = {}
    if canal:
        changes["channel_id"] = canal.id
    if descripcion is not None:
        changes["description"] = "" if descripcion.strip() == "-" else descripcion[:500]
    if imagen is not None:
        if imagen.strip() == "-":
            changes["image_url"] = None
        elif valid_url(imagen):
            changes["image_url"] = imagen
        else:
            return await interaction.followup.send("❌ La imagen debe ser una URL http(s)://")
    if emoji:
        changes["emoji"] = emoji
    if color:
        col = parse_color(color)
        if col is None:
            return await interaction.followup.send("❌ Color inválido. Usa formato hex, ej. `#ff5500`.")
        changes["color"] = col

    if not changes:
        return await interaction.followup.send("ℹ️ No indicaste nada que cambiar.")
    update_event(ev["id"], **changes)
    ev = get_event(interaction.guild_id, nombre)
    await interaction.followup.send("✅ Evento actualizado. Así se ve ahora:", embed=build_embed(ev))


@evento_group.command(name="borrar", description="Elimina un evento")
@app_commands.describe(nombre="Evento a eliminar", borrar_rol="¿Borrar también su rol de suscripción?")
@app_commands.checks.has_permissions(manage_guild=True)
async def evento_borrar(interaction: discord.Interaction, nombre: str, borrar_rol: bool = True):
    await interaction.response.defer(ephemeral=True)
    ev = get_event(interaction.guild_id, nombre)
    if not ev:
        return await interaction.followup.send(f"❌ No encontré el evento **{nombre}**.")

    role_note = ""
    if borrar_rol and ev["role_id"]:
        role = interaction.guild.get_role(ev["role_id"])
        if role:
            try:
                await role.delete(reason=f"Evento {ev['name']} eliminado")
                role_note = " y su rol"
            except discord.Forbidden:
                role_note = " (no pude borrar el rol, bórralo a mano)"
    delete_event(ev["id"])
    await interaction.followup.send(f"🗑️ Evento **{ev['name']}**{role_note} eliminado.")


@evento_group.command(name="probar", description="Publica un embed de prueba (sin etiquetar a nadie)")
@app_commands.describe(nombre="Evento a probar")
@app_commands.checks.has_permissions(manage_guild=True)
async def evento_probar(interaction: discord.Interaction, nombre: str):
    await interaction.response.defer(ephemeral=True)
    ev = get_event(interaction.guild_id, nombre)
    if not ev:
        return await interaction.followup.send(f"❌ No encontré el evento **{nombre}**.")
    channel = await get_channel(ev["channel_id"])
    if channel is None:
        return await interaction.followup.send("❌ No puedo acceder al canal configurado.")
    try:
        await channel.send(embed=build_embed(ev, test=True), allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        return await interaction.followup.send(f"❌ No pude enviar el mensaje: {e}")
    await interaction.followup.send(f"✅ Prueba enviada a {channel.mention}.")


for _cmd in (evento_rotacion, evento_editar, evento_borrar, evento_probar):
    _cmd.autocomplete("nombre")(evento_autocomplete)


# --------------------------------------------------------------------------- #
# Comandos para todos los miembros
# --------------------------------------------------------------------------- #
@bot.tree.command(name="eventos", description="Muestra los eventos de Aion 2 y cuándo son")
@app_commands.guild_only()
async def eventos_cmd(interaction: discord.Interaction):
    events = list_events(interaction.guild_id)
    if not events:
        return await interaction.response.send_message("Aún no hay eventos configurados.", ephemeral=True)

    embed = discord.Embed(title="📅 Eventos de Aion 2", color=DEFAULT_COLOR)
    for e in events:
        rot = f"cada {fmt_minutes(e['interval_min'])}" if e["interval_min"] else "evento único"
        embed.add_field(
            name=f"{e['emoji']} {e['name']}",
            value=f"Próximo: <t:{e['next_start']}:t> (<t:{e['next_start']}:R>)\n🔁 {rot}",
            inline=False,
        )
    embed.set_footer(text="Usa /suscribirse para recibir avisos")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="suscribirse", description="Recibe avisos de un evento (te asigno su rol)")
@app_commands.describe(evento="Evento al que quieres suscribirte")
@app_commands.guild_only()
async def suscribirse(interaction: discord.Interaction, evento: str):
    await interaction.response.defer(ephemeral=True)
    ev = get_event(interaction.guild_id, evento)
    if not ev:
        return await interaction.followup.send(f"❌ No encontré el evento **{evento}**. Mira la lista con /eventos.")
    try:
        role = await ensure_role(interaction.guild, ev)
        if role in interaction.user.roles:
            return await interaction.followup.send(f"ℹ️ Ya estás suscrito a **{ev['name']}**.")
        await interaction.user.add_roles(role, reason="Suscripción a evento")
    except discord.Forbidden:
        return await interaction.followup.send(
            "❌ No tengo permiso para darte el rol. Un admin debe revisar que mi rol "
            "tenga **Gestionar roles** y esté por encima de los roles de eventos."
        )
    await interaction.followup.send(f"🔔 ¡Listo! Te avisaré de **{ev['name']}** con {role.mention}.")


@bot.tree.command(name="desuscribirse", description="Deja de recibir avisos de un evento")
@app_commands.describe(evento="Evento del que quieres desuscribirte")
@app_commands.guild_only()
async def desuscribirse(interaction: discord.Interaction, evento: str):
    await interaction.response.defer(ephemeral=True)
    ev = get_event(interaction.guild_id, evento)
    if not ev:
        return await interaction.followup.send(f"❌ No encontré el evento **{evento}**.")
    role = interaction.guild.get_role(ev["role_id"]) if ev["role_id"] else None
    if role is None or role not in interaction.user.roles:
        return await interaction.followup.send(f"ℹ️ No estabas suscrito a **{ev['name']}**.")
    try:
        await interaction.user.remove_roles(role, reason="Desuscripción de evento")
    except discord.Forbidden:
        return await interaction.followup.send("❌ No tengo permiso para quitarte el rol. Avisa a un admin.")
    await interaction.followup.send(f"🔕 Listo, ya no recibirás avisos de **{ev['name']}**.")


suscribirse.autocomplete("evento")(evento_autocomplete)
desuscribirse.autocomplete("evento")(evento_autocomplete)


# --------------------------------------------------------------------------- #
# Manejo de errores y arranque
# --------------------------------------------------------------------------- #
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "🚫 Necesitas el permiso **Gestionar servidor** para usar este comando."
    else:
        log.error("Error en comando", exc_info=error)
        msg = "⚠️ Ocurrió un error inesperado. Inténtalo de nuevo."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Falta DISCORD_TOKEN. Crea un archivo .env (ver .env.example).")
    bot.run(TOKEN)
