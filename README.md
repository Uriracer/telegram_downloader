# Telegram Downloader

Recupera **els fitxers que tens desats als teus xats i canals de Telegram** (les teves còpies de
seguretat de sèries, pel·lícules i vídeos) i deixa'ls ordenats a la biblioteca de **Jellyfin**,
directament o a través de **Sonarr i Radarr**. Germà de
[3Cat Downloader](https://github.com/Uriracer/3cat-downloader): mateixa arquitectura, interfície web
en català i imatge Docker per a amd64 i arm64.

Fet a partir del que va passar recuperant 1.040 capítols d'*El detectiu Conan* (~550 GB de RAR) amb
`tdl` i scripts a mà: noms de fitxer caòtics, RAR multipart, connexions encallades, represes que no
reprenien, fitxers que es tornaven a baixar i un NAS que de vegades no estava muntat.

## Què fa (i què farà)

| Fase | Estat | Què |
|---|---|---|
| 0 | ✅ | Servidor web amb contrasenya, Docker amd64/arm64 amb `7zz`, **no escriure mai si el NAS no està muntat** |
| 1 | ✅ | Sessió de Telegram (**codi QR** o telèfon + 2FA), xats amb el **nom sencer**, índex de cada font a SQLite |
| 2 | ✅ | Reconeixement de noms de capítols, mode de comprovació (què falta, repetits, CRC amb «DC»…), TheTVDB |
| 3 | ✅ | Baixada robusta: represa per blocs, *watchdog*, paral·lelisme, Premium, velocitat i temps restant |
| 4 | ✅ | Descompressió (RAR5, multipart, ZIP, 7z) i col·locació de sèries a la biblioteca amb `.nfo` |
| 5 | ⏳ | Sonarr i Radarr (indexador Newznab + client SABnzbd) i prova amb Sonarr i Radarr reals |
| 6 | ⏳ | Títols en català (Viquipèdia) i verificació per títols |

## Inici ràpid (Docker Compose)

Afegeix el servei al teu `docker-compose.yml` (exemple complet a
[deploy/docker-compose.yml](deploy/docker-compose.yml); per a un stack com el de
[3Cat Downloader](https://github.com/Uriracer/3cat-downloader), amb `/mnt/Plex:/data`, hi ha el
fragment [deploy/media-stack-snippet.yml](deploy/media-stack-snippet.yml)):

```bash
touch /mnt/Plex/.tgdl-nas          # un sol cop, amb el NAS muntat: el fitxer sentinella
docker compose up -d telegram-downloader
```

Obre `http://<ip-del-servidor>:8766/`, crea la contrasenya i segueix els passos:

1. **Credencials de l'API**: l'aplicació et diu com obtenir l'`api_id` i l'`api_hash` a
   [my.telegram.org](https://my.telegram.org) (cinc minuts, un sol cop).
2. **Inicia la sessió** escanejant el codi QR amb el mòbil (Configuració → Dispositius → Connecta un
   dispositiu).
3. **Tria el canal** on tens les còpies i afegeix-lo com a font: s'indexa sol.

Guia detallada: [docs/installacio.md](docs/installacio.md).

## Seguretat

- La sessió de Telegram (`/config/telegram.session`) **és el teu compte**: qui tingui el fitxer hi té
  accés. Es crea amb permisos 0600 i la carpeta `/config` amb 0700. No la copiïs enlloc.
- La interfície demana contrasenya; Sonarr, Radarr i els scripts fan servir la clau de l'API.
- Fes servir l'aplicació només per recuperar **els teus** fitxers.

## Desenvolupament

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
# Interfície amb un Telegram simulat (sense compte):
TGDL_FAKE_TELEGRAM=tests/fixtures/fake_telegram.json .venv/bin/python -m tgdl
```

Llicència [MIT](LICENSE). Inclou el binari `7zz` de [7-Zip](https://www.7-zip.org/) (Igor Pavlov,
LGPL amb la restricció unRAR; llicència a `/usr/local/share/doc/7zip/License.txt` dins la imatge).
