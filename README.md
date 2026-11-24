# Qullamaggie Fused v2 → Bot para BingX + Telegram

Port a Python de la estrategia de TradingView **"Qullamaggie Fused v2 – EP Long + Parabolic Short"**.
Escanea **todos los futuros perpetuos USDT‑M de BingX** al cierre de cada vela, aplica la estrategia y
te envía las **señales detalladas por Telegram**. Pensado para correr 24/7 en **Railway**.

> ⚠️ **Solo envía señales. No coloca órdenes.** Operar es decisión y riesgo tuyo. Esto no es asesoramiento financiero.

## Qué hace

- Analiza ~500‑700 pares (todos los perpetuos USDT de BingX). Excluye los contratos de acciones, forex,
  índices y materias primas que BingX lista con prefijo `NCSK/NCFX/NCCO/NCSI` (configurable).
- **EP Long** y **Parabolic Short**, con la misma lógica y los mismos valores por defecto que el Pine.
- **Cada señal llega con todo el detalle**: entrada, stop, TP1 (+1.8R), tamaño sugerido según tu riesgo,
  RVOL, ADR, gap, Opening Range, gatillo que saltó, operaciones del día, enlace a TradingView…
- **Y después te avisa de la gestión**, como haría la estrategia: TP1 alcanzado (cerrar 50 % y stop a BE),
  stop, stop en BE, salida por trail con la EMA10/20 diaria y salida por tiempo, con el resultado en R.
- Mantiene "posiciones virtuales" para replicar esa gestión (una por par, máx. 2 operaciones/día por par).
- Estado en `state.json`: si se reinicia, no repite señales y sigue las posiciones abiertas.

## Archivos (todos en la raíz del repositorio, sin carpetas)

| Archivo | Para qué sirve |
|---|---|
| `main.py` | Arranque, descubrimiento de pares, bucle alineado al cierre de vela |
| `strategy.py` | **La estrategia** (port del Pine): contexto, gatillos, stops, tamaño, gestión |
| `config.py` | Lee las variables de entorno (mismos valores por defecto que el Pine) |
| `bingx.py` | Cliente de la API pública de BingX (sin API keys; límite de peticiones y reintentos) |
| `notifier.py` · `messages.py` | Envío a Telegram y texto de los mensajes |
| `store.py` · `models.py` · `indicators.py` | Estado persistente, estructuras de datos, EMA/ATR/SMA como en Pine |
| `test_offline.py` | 38 pruebas sin red (escenarios, gestión, reinicios, servidores simulados…) |
| `requirements.txt` · `railway.json` · `Procfile` | Dependencias y arranque en Railway |
| `env.example` | Todas las variables, comentadas |
| `.gitignore` · `README.md` | — |

## Puesta en marcha

### 1) Crear el bot de Telegram
1. En Telegram habla con **@BotFather** → `/newbot` → copia el **token**.
2. Abre un chat con tu bot y pulsa **Start** (si no, no podrá escribirte).
3. Obtén tu **chat id**: escribe cualquier mensaje al bot y abre
   `https://api.telegram.org/bot<TU_TOKEN>/getUpdates`; busca `"chat":{"id":123456789,...}`.
   (Para un grupo: añade el bot, escribe algo y repite; el id es negativo. Para un canal: el bot debe ser
   administrador y el id empieza por `-100`.)

### 2) Subir a GitHub
Crea un repositorio y sube **todos los archivos a la raíz** (en la web: *Add file → Upload files*).
Incluye `.gitignore`; **no subas ningún `.env` ni el token**.

### 3) Desplegar en Railway
1. **New Project → Deploy from GitHub repo** y elige el repositorio. Railway detecta Python por
   `requirements.txt` y arranca con `railway.json` (`python main.py`). No necesita puerto ni dominio.
2. Pestaña **Variables** → añade como mínimo:
   - `TELEGRAM_BOT_TOKEN` = el token de BotFather
   - `TELEGRAM_CHAT_ID` = tu chat id
   - (opcional) `TIMEZONE=Europe/Madrid` para ver también tu hora local en los mensajes.
3. **Recomendado – Volume** para no perder las posiciones virtuales en cada redeploy: crea un Volume
   (clic derecho en el lienzo del proyecto → *Volume*), conéctalo al servicio con *Mount Path* `/data`
   y añade la variable `DATA_DIR=/data`.
4. Despliega y abre los **Logs**. Deberías ver algo así, y un mensaje "🤖 Bot Qullamaggie iniciado" en Telegram:
   ```
   Universo: 6xx símbolos (excluidos por filtro: nn)
   Las velas diarias de BingX abren a las 00:00 UTC
   Escaneo #1: 6xx símbolos OK, 0 con error, 0 eventos, 150.3s ...
   ```
   El primer escaneo tarda más (descarga también las velas diarias); los siguientes, ~1 petición por par.

### 4) Probar antes (opcional, en tu PC)
```bash
pip install -r requirements.txt
python test_offline.py                              # pruebas sin red
cp env.example .env                                 # y edítalo
DRY_RUN=true python main.py --once                  # un escaneo; escribe los mensajes en consola, sin Telegram
```
Con `SYMBOLS=BTC-USDT,ETH-USDT` analizas solo esos pares mientras pruebas.

## Variables de entorno

Todas están en `env.example` con su explicación. Equivalencias con los inputs del Pine:

| Pine (grupo) | Variable(s) |
|---|---|
| Setups activos | `ACTIVE_SETUP` (`BOTH`, `EP_LONG`, `PARABOLIC_SHORT`) |
| Sesión | `SESSION_OFFSET_MIN`, `OR_MINUTES`, `SKIP_FIRST_MIN`, `ENTRY_WINDOW_MIN`, `DAY_LEN_MIN`, `MAX_TRADES_DAY` |
| Riesgo | `RISK_PCT_EP`, `RISK_PCT_SHORT`, `MAX_POS_PCT`, `MAX_ADR_MULT`, `STOP_MODE` (`DAY`/`OR`/`ATR`), `SL_BUFFER_ATR` |
| EP Long | `EP_MIN_CHANGE`, `EP_MIN_ADR`, `EP_MIN_RVOL`, `EP_TRIGGER` (`ORH`/`VWAP_EMA`/`HH`) |
| Parabolic Short | `PS_MIN_GAIN`, `PS_LOOKBACK`, `PS_MIN_GREEN`, `PS_MIN_EXT`, `PS_TRIGGER` (`ORL`/`FIRST_RED`/`VWAP_FAIL`) |
| Gestión | `PARTIAL_R`, `PARTIAL_PCT`, `USE_TRAIL_EMA`, `TRAIL_EMA_LEN`, `MAX_HOLD_BARS` |
| *(nuevo)* | `TIMEFRAME`, `ACCOUNT_EQUITY`, `SYMBOLS`, `MIN_DAILY_VOL_USDT`, `NOTIFY_ARMED`, `HEARTBEAT_HOURS`, `DATA_DIR`… |

Consejos: `ACCOUNT_EQUITY` es el capital con el que se calcula el **tamaño sugerido** (pon el tuyo).
`MIN_DAILY_VOL_USDT=1000000` descarta las monedas demasiado ilíquidas para operar con tamaño.
`NOTIFY_ARMED=true` añade avisos cuando un par queda "EP armado" o "Parabolic activo" (más mensajes).

## Cómo replica el Pine (y en qué se diferencia)

Igual que el Pine: contexto EP (cambio/gap vs. cierre de ayer, ADR20, RVOL a esta hora con el 5 % mínimo de
fracción de día), latch de "EP armado" hasta el día siguiente, Opening Range, VWAP anclado al día, EMA21 y ATR14
(semillas y suavizado como `ta.ema`/`ta.rma`), datos diarios **solo de días cerrados** (`[1]`, sin repintado),
filtro de stop máximo en múltiplos de ADR (×1.2 extra en el short), tamaño por riesgo con tope de posición,
parcial a +R, stop a BE desde la vela siguiente al parcial, trail con el cierre diario vs. EMA10/20 evaluado al
abrir el día, máx. operaciones por día y una sola posición por par. Se evalúa siempre con velas **cerradas**.

Diferencias que conviene conocer:

- **Entrada al cierre de la vela de señal** (lo que verás en el mensaje). El backtest de TradingView entra en la
  apertura siguiente y aplica 3 ticks de slippage y 0,05 % de comisión: aquí no se modelan.
- **Equity fija** (`ACCOUNT_EQUITY`): el Pine usa el equity que va cambiando con el backtest.
- Si en una misma vela se tocan stop y TP1, se asume **el stop primero** (criterio prudente).
- Los datos son los de **BingX**, no los de TradingView: puede haber pequeñas diferencias de volumen (RVOL).
- Si **no tomas** una señal, el bot la sigue como posición virtual y no emite otra del mismo par hasta que
  salga (igual que el Pine mantendría la posición).
- Cripto es 24/7: el "gap" casi no existe y el día se parte a las 00:00 UTC (el bot lo autodetecta con las velas
  diarias de BingX). `SESSION_OFFSET_MIN=810` mueve la "apertura" a la de EE. UU., como en el Pine.
- Al reiniciar sin `state.json` el bot solo mira la última vela cerrada (no inunda con señales antiguas).

## Rendimiento y límites

- BingX permite 100 peticiones/10 s por IP en datos públicos; el bot usa 8/s (`RATE_PER_SEC`).
  Con ~600 pares, un escaneo tarda ~75 s: **usa 5m o más**. Con 1m/3m no da tiempo a escanear todo.
- Si el log avisa de que *"el escaneo tarda más que el timeframe"*, sube el timeframe o filtra pares.

## Problemas frecuentes

- **No llega nada a Telegram**: revisa el token y el chat id, y que hayas pulsado *Start* en el bot. Los errores
  de Telegram salen en el log (`Telegram rechazó el mensaje…`).
- **`HTTP 403` / `451` desde BingX**: bloqueo geográfico de la región de Railway; cambia la región del servicio.
- **`BingX responde mal` / muchos errores**: límite de peticiones compartido; baja `RATE_PER_SEC` a 5‑6.
- **Se pierden las posiciones al redesplegar**: falta el Volume (`DATA_DIR=/data`, ver paso 3).
- **Cuántos mensajes llegan**: con los valores por defecto, pocos por día (la estrategia es muy selectiva).
  Puedes comprobar que está vivo con el mensaje periódico "💓 Bot activo" (`HEARTBEAT_HOURS`).
