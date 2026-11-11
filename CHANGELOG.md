# Registre de canvis

Format basat en [Keep a Changelog](https://keepachangelog.com/ca/1.1.0/); versions [SemVer](https://semver.org/lang/ca/).

## [0.1.0] — en desenvolupament

### Fase 6: títols en català
- **Viquipèdia**: la font d'episodis pot indicar la seva llista d'episodis de la Viquipèdia en català
  (`wikipedia_list`, «Llista d'episodis d'El detectiu Conan»). Se'n llegeixen també les pàgines de temporades
  que hi enllaça, i de cada episodi el títol i la sinopsi, pel número japonès (el dels fitxers i de TheTVDB).
  Es desa a la base de dades una setmana.
- Els `.nfo` nous porten «887. En Kaito Kid i la caixa secreta. Primera part» i la sinopsi.
- **Posa els títols en català** (`POST /api/sources/{id}/titles`) actualitza els `.nfo` que ja ha creat
  l'aplicació (marca `<!-- tgdl -->`); els altres no es toquen mai.
- Al servidor: 154 `.nfo` actualitzats (887–1040), els 887 anteriors intactes.

### Fase 5: Radarr (pel·lícules)
- **Pel·lícules**: «DC-Movie 22», «DC Movie 24», «Animelliure_DC_Movie_01_v2…» es reconeixen (número de
  pel·lícula) i els volums `_part1.rar` (amb guió baix) s'agrupen bé: abans cada volum semblava un arxiu.
- **Col·lecció de TMDB** a les opcions de la font (`tmdb_collection`, 39199 per al Detectiu Conan): la
  pel·lícula N és l'N-èsima de la col·lecció per data d'estrena. Ve del servei de metadades de Radarr i es
  desa a la base de dades (inclou els títols traduïts, el català també).
- **Radarr**: l'indexador ofereix una pel·lícula per número amb el títol i l'any de TMDB
  (`Case.Closed.Zero.the.Enforcer.2018.CATALAN.720p.WEB-DL-TGDL`) i els ids de TMDB i IMDb; cerca per
  `tmdbid`, `imdbid` o títol, i novetats per a les categories 2000. Mai les que ja són a la biblioteca.
- Provat amb el Radarr real: les 28 pel·lícules del canal s'interpreten bé (títol, any, WEBDL-720p, català).

### Fase 5: Sonarr
- **Indexador Newznab** (`/arr/newznab/api`): Sonarr hi busca per l'id de TheTVDB de les fonts (o pel títol)
  i rep un llançament per episodi (la millor còpia, tots els volums) amb la mida real i un nom que entén:
  `Detective.Conan.S26E01.Títol.CATALAN.720p.WEB-DL-TGDL`.
- **Mai el que ja és a la biblioteca**: Sonarr tenia els fitxers del Conan com a SDTV/HDTV i hauria
  «millorat» ~850 episodis tornant a baixar els mateixos fitxers.
- **Numeració scene** (TheXEM): Sonarr busca el Conan com a `season=1&ep=887`; l'episodi amb aquest número
  absolut es retorna anomenat `S01E887`.
- **Client SABnzbd** (`/sabnzbd/api`): el llançament triat es baixa, es descomprimeix a
  `<baixades>/tv/<llançament>/` i Sonarr l'importa; després l'esborra i els fitxers queden «A la biblioteca».
  Les feines es desen a la base de dades (migració 2) i es reprenen després d'un reinici. La col·locació
  directa de la fase 4 no toca els episodis que ha demanat Sonarr.
- Quan no hi ha res a oferir, la llista de novetats porta un element de mostra d'una sèrie inexistent: si és
  buida, Sonarr no deixa desar l'indexador.
- Configuració: com connectar Sonarr. Dependència nova: `python-multipart` (els NZB que envia Sonarr).

### Fase 4: biblioteca (sèries)
- **Col·locació a la biblioteca**: cada episodi baixat (tots els volums d'un arxiu, o un vídeo sol) es
  descomprimeix amb `7zz` i el vídeo va a `<sèrie>/Season N/<Sèrie> - S26E01 - 0887.mp4` (número absolut
  amb la numeració absoluta), el format que ja hi havia; la carpeta de temporada es crea si cal.
- `.nfo` de cada episodi: títol (el del nom del fitxer, «DC-1035 (Escac i mat)», o el de TheTVDB),
  temporada, episodi, data d'emissió i número absolut; marcat amb `<!-- tgdl -->` perquè la fase 6 en
  pugui posar el títol en català. Llegible per tothom, com els vídeos.
- Els volums recuperen el nom original (enllaços) abans de descomprimir: els de `tdl` porten un prefix i
  7-Zip no trobaria el `.part2.rar`.
- **No se sobreescriu mai res**: si l'episodi ja hi és amb un fitxer diferent, s'avisa. Un arxiu amb dos
  vídeos, amb contrasenya o al qual falta un volum es deixa baixat amb el motiu.
- Els arxius s'esborren quan el seu episodi ja és a la biblioteca (opció *Esborra els arxius*).
- Automàtic en acabar de baixar per a les fonts amb destí «Directament a la biblioteca»; per a la resta,
  botó **Col·loca a la biblioteca** a la font (`POST /api/sources/{id}/place`, amb `limit` per provar-ho
  amb pocs). La targeta de la font mostra el progrés i els errors de l'última col·locació.
- Baixades: en aturar l'aplicació ja no es comença cap fitxer nou.

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
