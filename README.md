# iwiRacer

Usa mandos de Wii en Linux como un mando Xbox 360 virtual. Pensado para **STAR WARS: Galactic Racer** con Proton, pero sirve para cualquier juego que acepte un mando Xbox.

No toca los archivos del juego. El juego solo ve un mando Xbox normal.

## Qué hace

- Wiimote y Nunchuk: botones, stick y vibración.
- Movimiento: inclinar o agitar los mandos, y puntero IR.
- Modo podracer: cada mando es una palanca de motor.
- Dos Wiimotes sin Nunchuk, como un solo mando.
- MotionPlus: giroscopio para ángulos más estables y golpes de muñeca.
- Modo ratón: Home cambia entre mando y ratón.
- Interfaz para configurarlo todo, con vista del mando en directo.

## Estado

| Función | Probado con |
|---|---|
| Botones y vibración | Mando real |
| Movimiento, IR, ratón | Datos simulados |
| Dos Wiimotes, MotionPlus | Datos simulados. MotionPlus viene apagado: falta comprobar el sentido de sus ejes |
| STAR WARS: Galactic Racer | Aún sin probar |

Solo se ha probado en Arch Linux.

## Instalar

1. Instala lo necesario. Hace falta Python 3.11 o superior.

   ```sh
   # Arch
   sudo pacman -S --needed git python base-devel bluez bluez-utils pyside6
   # Debian / Ubuntu
   sudo apt install git python3-venv python3-dev build-essential bluez
   # Fedora
   sudo dnf install git python3-devel gcc bluez
   ```

2. Descarga e instala. El instalador pide `sudo` para los permisos de los dispositivos.

   ```sh
   git clone https://github.com/Kalaminator/iwiRacer.git
   cd iwiRacer
   ./install.sh
   ```

3. Cierra la sesión y vuelve a entrar si el mando virtual no aparece.

## Conectar un mando de Wii

Solo hace falta la primera vez. Pulsa el botón rojo **SYNC**, bajo la tapa de las pilas, y ejecuta:

```sh
bluetoothctl
scan on
```

Cuando aparezca `Nintendo RVL-CNT-01` (o `RVL-CNT-01-TR`), copia su dirección y escribe:

```sh
pair DIRECCION
trust DIRECCION
connect DIRECCION
scan off
exit
```

Las siguientes veces basta con pulsar **1 + 2** en el mando.

## Configurar

Abre **iwiRacer** en el menú de aplicaciones. Pulsa **Guardar y aplicar** después de cada cambio.

- **Botones, Stick, Vibración:** qué hace cada control.
- **Movimiento:** convierte el movimiento en stick o botones.
- **Dos Wiimotes:** marca la opción, conecta los dos, inclina uno y mira las barras. Si se mueve la barra equivocada, marca **Intercambiar izquierda y derecha**.
- **MotionPlus:** deja el Wiimote quieto 1 segundo al conectarlo.

## En Steam

En el juego: **Propiedades → Mando → Desactivar Steam Input**. Si el juego no ve el mando, prueba con Steam Input activado.

## Pruebas

```sh
.venv/bin/python test_axis.py

# Con mandos simulados. Apaga antes el servicio y los mandos reales.
systemctl --user stop wiimote2xbox
.venv/bin/python test_two_wiimotes.py
systemctl --user start wiimote2xbox
```

## Licencia

GPL-3.0. Consulta [LICENSE](LICENSE).

La mezcla de palancas del modo podracer está adaptada de Galactic Racer HOSAS/HOTAS Binder, © 2026 JaffaQuake, GPLv3. La lógica de zona muerta se inspira en [x360ce](https://github.com/x360ce/x360ce).

Proyecto no oficial. Sin relación con Nintendo, Microsoft, Lucasfilm ni los creadores del juego.
