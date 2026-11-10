# Registre de canvis

Format basat en [Keep a Changelog](https://keepachangelog.com/ca/1.1.0/); versions [SemVer](https://semver.org/lang/ca/).

## [0.1.0] — en desenvolupament

### Fase 1: sessió de Telegram, xats i índex
- Inici de sessió amb el compte d'usuari de Telegram (MTProto, Telethon) des del navegador: **codi QR**
  (es renova sol) o telèfon + codi, amb la contrasenya de verificació en dos passos si cal.
- La sessió es desa (`telegram.session`, permisos 0600) i es recupera en arrencar: després d'un tall de
  llum no cal tornar a entrar.
- Guia a la interfície per obtenir l'`api_id` i l'`api_hash` de https://my.telegram.org.
- Detecció de Telegram Premium (i dels canvis d'estat).
- Llista de xats i canals amb el **nom sencer** i l'ID; qualsevol es pot afegir com a font.
- Índex de cada font a SQLite (nom original, mida, data, text i ID del missatge) i refresc periòdic;
  «Reindexa sencer» detecta els missatges esborrats. L'estat de cada fitxer no depèn mai del que hi ha
  al disc.
- Opcions per font: contingut (sèries/pel·lícules/altres), destí (Sonarr/Radarr o directament a la
  biblioteca) i idioma de l'àudio.

### Fase 0: base
- Servidor web en català amb contrasenya, clau d'API i protecció CSRF (com 3Cat Downloader).
- **Fitxer sentinella del NAS**: no s'escriu res mentre no hi és (si el NAS no està muntat, la carpeta
  és al disc local i s'ompliria). L'entrypoint de Docker tampoc no crea la carpeta de baixades.
- Imatge Docker amd64 + arm64 amb el **7zz oficial** de 7-zip.org (obre RAR5 i RAR multipart; el `7zip`
  de Debian trixie no).
- Proves amb un Telegram simulat, CI a Linux i Windows.
