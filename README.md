# script-bienestar

Cliente de **un solo intento** para la reserva de cupos de comedor de la UNSAAC
(`http://bienestar.unsaac.edu.pe/`, servido en **HTTP plano**, sin HTTPS).

## Flujo que implementa

```
1. GET  /                                          -> fija la cookie PHPSESSID
2. GET  /simple-php-captcha.php?_CAPTCHA&t=<time>  -> imagen captcha (PNG)
3. Resolver el captcha con ddddocr
4. POST /registro.php  {codigo, pass, cap}         -> JSON
5. Interpretar {respuesta: si|no, mensaje, alumno, nombre, fecharegistro}
```

- `registro.php` valida el **captcha primero**.
- Solo se reintenta ante fallos de **red** (timeout / conexión reseteada), nunca
  ante un rechazo lógico (`respuesta == "no"`). Modo *prueba única*.

## Requisitos

```powershell
pip install -r requirements.txt
```

## Configuración

Copia la plantilla y coloca tus datos reales:

```powershell
Copy-Item config.example.json config.json
```

`config.json` (no se sube a git, ver `.gitignore`):

```json
{
  "codigo": "123456",
  "pass": "12345",
  "base_url": "http://bienestar.unsaac.edu.pe",
  "captcha_url": "/simple-php-captcha.php?_CAPTCHA&t={t}",
  "registro_url": "/registro.php"
}
```

- `codigo`: tu código de estudiante (6 dígitos).
- `pass`: la clave de 5 dígitos que aparece en el **voucher** de pago por matrícula.

## Uso

```powershell
# Prueba de captcha sin enviar la reserva (recomendado primero)
python bienestar.py --dry-run

# Reserva real (un intento)
python bienestar.py

# Forzar un captcha concreto (si el OCR falla)
python bienestar.py --manual 1a2B3

# Usar otro archivo de configuración
python bienestar.py --config otro.json
```

## Salida y depuración

Todo intento deja rastro en `debug/`:

- `debug/captcha.png`   → la imagen descargada (revisa que no esté vacía).
- `debug/resultado.json` → respuesta cruda del servidor.

Ejemplo de éxito:

```
================ RESERVA EXITOSA ================
  Mensaje        : ...
  Codigo alumno  : 123456
  Nombre         : APELLIDOS NOMBRES
  Fecha y hora   : 26-09-2026 18:00:05
=================================================
```

## Notas importantes

- La web va por **HTTP sin cifrado**: tu código y clave viajan en texto plano.
- El servidor es **intermitente** y aplica **bloqueo por IP** ante ráfagas de
  peticiones (devuelve `Connection reset`). Si lo ves, espera unos minutos.
- Si `debug/captcha.png` queda en 0 bytes, es señal de bloqueo de red o de que
  la sesión no generó captcha: espera y reintenta.
- `simple-php-captcha` es alfanumérico distorsionado; ddddocr suele acertarlo,
  pero no al 100%. Usa `--manual` como respaldo.
