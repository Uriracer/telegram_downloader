# Registre de canvis

Format basat en [Keep a Changelog](https://keepachangelog.com/ca/1.1.0/); versions [SemVer](https://semver.org/lang/ca/).

## [0.1.0] — en desenvolupament

### Fase 3: baixades
- **Cua de baixades**: «Baixa el que falta» posa a la cua la millor còpia completa de cada episodi que no és
  a la biblioteca (un sol cop cada volum penjat dues vegades); també fitxer a fitxer. Pel·lícules i altres:
  tot menys les fotos.
- **Represa per blocs**: cada fitxer es baixa a blocs de 512 KB escrits al seu lloc dins d'un `.part`, i un
  `.part.json` diu quins hi són. Després d'una pausa, d'un reinici o d'un tall de llum només es baixa el que
  falta; un `.part` sense el seu `.part.json` (com els de `tdl`) es comença de nou. Els fitxers començats
  passen davant de la cua.
- **Watchdog**: cada petició té un temps màxim (*Reconnecta si un fitxer no avança en*); si Telegram no
  respon es torna a demanar, amb pauses creixents. Després de 8 errors seguits el fitxer passa a «Error» amb
  el motiu i la resta de la cua continua. `FLOOD_WAIT` s'espera sense comptar com a error.
- **Paral·lelisme**: *Fitxers alhora* i *Connexions per fitxer* (blocs del mateix fitxer alhora).
- **Mai al disc equivocat**: es comprova el sentinella del NAS abans de cada escriptura; si desapareix, les
  baixades tornen a la cua amb el que tenien i no s'escriu res fins que torna. No es comença cap baixada amb
  menys d'1 GB lliure, ni un fitxer que no hi cap.
- Pestanya **Baixades**: progrés, velocitat, temps restant, connexions, reintents i esperes de Telegram,
  avís si el compte no és Premium, **Pausa** / **Reprèn** (es recorda després de reiniciar) i
  **Treu de la cua** (el que ja s'ha baixat es guarda).
- Els fitxers queden a `<carpeta temporal>/src-<font>/` amb el nom original (sanejat) i passen a «Baixat».
- API: `POST /api/sources/{id}/queue` (`msg_ids` o `missing`), `POST /api/sources/{id}/unqueue`,
  `GET /api/downloads`, `POST /api/downloads/pause` i `/resume`; esdeveniment SSE `downloads`.

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
- **Biblioteca** (la carpeta que llegeix Jellyfin): la troba per `[tvdbid-N]` o la tries tu, i marca com a
  «A la biblioteca» els fitxers dels episodis que ja hi són, encara que el zip s'hagi esborrat: no es
  baixaran mai. Entén `S17E11`, `501. Títol.mkv` i `Season 17/11 - Títol.mkv`. Es comprova sola després de
  cada indexació i quan canvien les opcions de la font.
- **Navegador de carpetes** (només dins del NAS) per triar la carpeta de la sèrie, la de `tdl` i les de la
  configuració.
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
