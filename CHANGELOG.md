# Registre de canvis

Format basat en [Keep a Changelog](https://keepachangelog.com/ca/1.1.0/); versions [SemVer](https://semver.org/lang/ca/).

## [0.1.0] — en desenvolupament

### Fase 2: revisió abans de baixar
- **Reconeixement dels noms** dels quatre formats de les còpies del Detectiu Conan (`Animelliure_DC_001_…`,
  `13x501_…`, `10x424-…`, `…_Conan_525_…`, `DC-928 (…) [CRC].part1.rar`) i dels habituals (`S01E05`, `1x05`).
  El CRC es treu abans de buscar el número: `[5DC7CC1E]` no és mai el capítol 7. Versions (`V2`),
  resolucions (`720p`) i meitats d'una història (`(I)`, `(II)`) tampoc.
- Numeració per font: **per temporades** (`1x05` = T1 E5) o **absoluta** (anime: `13x501` = episodi 501).
- **TheTVDB** (via SkyHook, com Sonarr, sense clau): el número absolut japonès es converteix a temporada i
  episodi (501 = S17E11). Es desa a la base de dades i serveix encara que no hi hagi connexió.
- **Revisió** de cada font: agrupa els volums dels arxius multipart, i diu quins episodis falten, quins
  estan repetits (es queda la versió més alta), quins no tenen número, quins no són a TheTVDB i quins
  arxius estan incomplets. No baixa ni esborra res.
- **Biblioteca**: troba la carpeta de la sèrie per `[tvdbid-N]` i marca com a «A la biblioteca» els fitxers
  dels episodis que ja hi són: no es baixaran mai.
- **Importació del que ha baixat `tdl`** (`<canal>_<missatge>_<nom>`): si la mida coincideix amb la de
  Telegram, el fitxer queda com a baixat; si no (baixada a mitges), es tornarà a baixar. No es mou res.

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
