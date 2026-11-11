# Instal·lació i primer inici de sessió

Aquesta guia parteix d'un servidor com el de referència: Proxmox, contenidor LXC amb Debian 13 i Docker
Compose a `/opt/media/docker-compose.yml`, amb la biblioteca en un NAS per NFS muntat a `/mnt/Plex`
(`Series/`, `Pelicules/`, `Descarregues/`) i Sonarr i Radarr amb el volum `/mnt/Plex:/data`.

## 1. El fitxer sentinella del NAS

Si el NAS no està muntat, `/mnt/Plex` és una carpeta buida **del disc arrel** (32 GB). Docker la
munta igualment dins el contenidor i qualsevol baixada l'ompliria. Per evitar-ho, crea un fitxer que
només existeix quan el NAS hi és:

```bash
mount | grep /mnt/Plex          # comprova que està muntat
touch /mnt/Plex/.tgdl-nas
```

i digues a l'aplicació on és (`TGDL_STORAGE_SENTINEL=/data/.tgdl-nas`). Mentre no el vegi:

- no escriu res (ni crea carpetes);
- la interfície mostra un avís vermell;
- en arrencar no falla: espera que el NAS arribi (útil després d'un tall de llum, si el NAS triga
  més a arrencar que el servidor).

## 2. El servei a Docker Compose

### Amb el script (recomanat)

[deploy/install-lxc.sh](../deploy/install-lxc.sh) fa els passos 1 i 2 sol, dins el contenidor LXC i
com a root:

```bash
bash install-lxc.sh
```

1. Comprova que `/mnt/Plex` està muntat (si no, s'atura sense tocar res) i crea el sentinella.
2. Baixa el codi a `/opt/media/src/telegram-downloader` (o l'actualitza).
3. Fa una còpia de `docker-compose.yml` (`docker-compose.yml.bak-<data>`), hi afegeix el servei
   **un sol cop**, amb l'ancoratge `&env` i `/mnt/Plex:/data`, i el valida amb
   `docker compose config`. Si el resultat no és vàlid, restaura la còpia.
4. Construeix la imatge i engega **només** aquest servei: la resta de l'stack no es reinicia.

Per actualitzar a una versió nova, torna'l a executar. Variables: `COMPOSE_FILE`, `NAS_DIR`,
`REPO_URL`, `BRANCH`, `PORT`.

Mentre el repositori sigui privat, el contenidor necessita permís de lectura: una clau de desplegament
(`REPO_URL=git@github.com:Uriracer/telegram_downloader.git`) o un token de GitHub de només lectura, que
`git` et demanarà com a contrasenya.

### A mà

Afegeix a `/opt/media/docker-compose.yml`, sota `services:`, el fragment de
[deploy/media-stack-snippet.yml](../deploy/media-stack-snippet.yml):

```yaml
  telegram-downloader:
    image: ghcr.io/uriracer/telegram-downloader:latest
    container_name: telegram-downloader
    environment:
      <<: *env
      UMASK: "002"
      TGDL_DOWNLOAD_DIR: /data/Descarregues/telegram
      TGDL_LIBRARY_TV: /data/Series
      TGDL_LIBRARY_MOVIES: /data/Pelicules
      TGDL_STORAGE_SENTINEL: /data/.tgdl-nas
    volumes: [./config/telegram-downloader:/config, /mnt/Plex:/data]
    ports: ["8766:8766"]
    restart: unless-stopped
    stop_grace_period: 30s
```

Mentre no hi hagi cap versió publicada a GHCR, canvia `image:` per
`build: https://github.com/Uriracer/telegram_downloader.git#main`.

```bash
cd /opt/media && docker compose up -d telegram-downloader
docker compose logs -f telegram-downloader      # només cal el primer cop
```

`restart: unless-stopped` fa que torni a arrencar sol després d'un tall de llum, i reprèn la sessió
de Telegram sense demanar res.

| Variable | Per a què | Exemple |
|---|---|---|
| `TGDL_DOWNLOAD_DIR` | On deixa el que importen Sonarr i Radarr | `/data/Descarregues/telegram` |
| `TGDL_TEMP_DIR` | Fitxers a mitges i descompressió (buit = `<baixades>/.tmp`, al NAS) | |
| `TGDL_LIBRARY_TV`, `TGDL_LIBRARY_MOVIES` | Biblioteques per a la col·locació directa | `/data/Series` |
| `TGDL_STORAGE_SENTINEL` | Fitxer que demostra que el NAS està muntat | `/data/.tgdl-nas` |
| `TGDL_API_ID`, `TGDL_API_HASH` | Credencials de my.telegram.org (opcional; si no, des del navegador) | |
| `TGDL_PASSWORD` | Contrasenya fixa de la interfície (si no, la crees el primer cop) | |
| `TGDL_API_KEY` | Clau de l'API fixa (si no, se'n genera una) | |
| `PUID`, `PGID`, `UMASK` | Propietari dels fitxers (el mateix que Sonarr i Radarr) | `1000`, `1000`, `002` |

Una variable definida al Compose no es pot canviar des de la interfície (surt bloquejada).

## 3. Les credencials de l'API de Telegram

Telegram demana que cada aplicació que entra com a usuari tingui un `api_id` i un `api_hash`. L'eina
`tdl` no els demanava perquè fa servir els de l'aplicació oficial d'escriptori.

1. Entra a [my.telegram.org](https://my.telegram.org) amb el teu número (el codi arriba a l'aplicació
   de Telegram).
2. **API development tools**.
3. App title «Telegram Downloader», Short name «tgdl», Platform «Desktop».
4. Copia l'**App api_id** i l'**App api_hash** a la pantalla «Telegram» de l'aplicació.

## 4. Iniciar la sessió

A la pantalla **Telegram**:

- **Amb codi QR** (recomanat): al mòbil, Configuració → Dispositius → Connecta un dispositiu, i
  escaneja el codi. Es renova sol cada pocs segons.
- **Amb el número de telèfon**: escriu-lo amb el prefix (`+34…`); el codi arriba a l'aplicació de
  Telegram.
- Si tens la verificació en dos passos, et demanarà la contrasenya (no es desa).

Si el compte és **Premium**, ho veuràs al costat del nom. Sense Premium, Telegram limita la velocitat
de baixada després de baixar molt.

## 5. Afegir una font

Sota el compte surten tots els xats amb el nom sencer. Filtra'ls, tria el canal on tens les còpies i
fes **Afegeix com a font**. L'aplicació llegeix tots els missatges amb fitxers (nom original, mida,
data, text) i ho desa a la base de dades; després el torna a revisar cada hora (configurable) per
trobar fitxers nous. **Reindexa sencer** torna a llegir tot el canal i marca els missatges esborrats.

A **Opcions de la font** tria què conté (sèries, pel·lícules), on ha d'anar (a través de Sonarr i
Radarr o directament a la biblioteca) i l'idioma de l'àudio (català, castellà…).

## 6. Revisar una font abans de baixar

A **Opcions de la font**:

- **Numeració dels noms**: *absoluta* per a l'anime (el Detectiu Conan: `13x501` és l'episodi 501), *per
  temporades* per a la resta (`1x05` és la temporada 1, episodi 5).
- **Id de TheTVDB**: el número de la sèrie a thetvdb.com, el mateix de la carpeta de Jellyfin
  (`Detectiu Conan (1996) [tvdbid-72454]` → `72454`).
- **Carpeta de la sèrie a la biblioteca**: la que llegeix Jellyfin. Buida = la que porta `[tvdbid-…]` dins de
  `TGDL_LIBRARY_TV`; si la teva no el porta, tria-la amb **Tria…** (el navegador només mostra carpetes
  del NAS). Els episodis que hi ha (`S17E11`, `501. Títol.mkv`, `Season 17/11 - Títol.mkv`) es marquen
  sols com a «A la biblioteca» després de cada indexació, encara que el zip ja s'hagi esborrat.

Al tauler **Revisió abans de baixar**:

1. **Revisa**: llista els episodis que falten, els repetits (V2/V3, o el mateix fitxer penjat dues vegades:
   es baixarà només el marcat amb ✔), els fitxers sense número, els que no són a TheTVDB i els arxius
   multipart als quals falta algun volum. No canvia res.
2. **Torna a mirar la biblioteca**: el mateix que es fa sol després d'indexar (útil si acabes de posar-hi
   episodis).
3. **Importa el que ha baixat tdl**: tria la carpeta amb **Tria…** o escriu-la tal com la veu el contenidor (`/mnt/Plex/_conan/zips`
   és `/data/_conan/zips`). Primer et diu què farà; els fitxers no es mouen. Els que tenen una mida diferent
   de la de Telegram es tornaran a baixar.

## 7. Baixar

- **Baixa el que falta** (a la font): posa a la cua la millor còpia de cada episodi que no és a la
  biblioteca: la versió més alta i completa, i un sol cop cada volum penjat dues vegades. Els arxius
  sense número o als quals falta algun volum no s'hi posen (et diu quants n'hi ha); els pots baixar d'un
  en un amb **Baixa** a la llista de **Fitxers**. Per a pel·lícules i altres continguts s'hi posa tot.
- La pestanya **Baixades** mostra els fitxers que s'estan baixant, la velocitat i el temps que queda.
  **Pausa** els atura tots (el que ja s'ha baixat es guarda) i **Treu de la cua** en treu un.
- Es baixen **Fitxers alhora** fitxers a la vegada, cadascun amb **Connexions per fitxer** connexions
  (Configuració). Sense Telegram Premium, més connexions ajuden poc: Telegram limita la velocitat.
- Els fitxers baixats queden a la carpeta temporal (`<baixades>/.tmp/src-<font>/`, al NAS) amb el nom
  original, tots els volums d'un arxiu junts, a punt per descomprimir.

Cada fitxer es baixa a blocs de 512 KB i, al costat del fitxer a mitges (`.N.part`), un `.N.part.json`
diu quins blocs ja hi són: després d'aturar el contenidor o d'un tall de llum es continua on s'havia
quedat. Si Telegram no respon en **Reconnecta si un fitxer no avança en** segons, es torna a demanar el
bloc; després de 8 intents seguits, el fitxer passa a «Error» amb el motiu i la cua continua. Si el NAS
desapareix a mitja baixada, s'atura tot i es reprèn sol quan torna.

## Problemes habituals

| Què veus | Per què | Què fer |
|---|---|---|
| «El disc de xarxa no està muntat» | No hi ha el fitxer sentinella | Munta el NAS (`mount -a`) i comprova `ls /mnt/Plex/.tgdl-nas` |
| «L'api_id o l'api_hash no són vàlids» | S'han copiat malament | Torna a copiar-los de my.telegram.org a Configuració |
| Telegram «demana esperar N s» | Massa intents (FLOOD_WAIT) | Espera; l'aplicació ho torna a provar sola |
| «La sessió s'ha tancat des d'un altre dispositiu» | L'has tancat des de Telegram → Dispositius | Torna a escanejar el QR |
| La interfície no carrega des d'un altre equip | Port no publicat o tallafoc | Comprova `ports: ["8766:8766"]` |
| La revisió diu «No hi ha cap carpeta amb [tvdbid-…]» | La carpeta de la sèrie no porta l'id | Posa-li `[tvdbid-N]` al nom, com espera Jellyfin |
| Molts fitxers «sense número» | Un format de nom nou | Envia'n uns quants exemples: s'afegirà |
| «ja s'està executant amb aquesta carpeta de dades» | Dos contenidors amb el mateix `/config` | Atura'n un: compartirien la sessió de Telegram |
| «Queda menys d'1 GB lliure al disc» | El disc de les baixades és ple | Allibera espai: les baixades continuen soles |
| Un fitxer en «Error» amb «Telegram no ha respost…» | Connexió dolenta durant una estona | **Torna-ho a provar** a la llista de fitxers: continua on era |
| «El missatge ja no existeix» | L'han esborrat del canal | **Reindexa sencer** el marca com a esborrat |
