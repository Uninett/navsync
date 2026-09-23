# Testing
Akkurat nå testes navsync kun interaktivt

## Oppsett av lokale NAV- og Netbox-instanser
For å teste interaktivt (gjennom et REPL eller kommandolinjen) må man kunne snakke med både NAV og Netbox fra skriptet:

- Spinn opp en lokal NAV-instans (jeg bruker `docker-compose.yaml` fra
  [NAV kodelageret](https://github.com/Uninett/nav/blob/master/docker-compose.yml) her)
- Spinn opp en lokal Netbox-instans som forklart i [gitlab.sikt.no/security/netbox-docker](https://gitlab.sikt.no/security/netbox-docker).
  Jeg hadde en port-konflikt mellom NAV-web og Netbox-web, så jeg endret
  ```yaml
  netbox:
    ports:
      - "8080:8080"
  ```
  til
  ```yaml
  netbox:
    ports:
      - "8081:8080"
  ```
- Sett opp config-filer som samsvarer med oppsettet ovanfor:

  Kjør
  ```bash
  openssl genrsa -out /path/to/private_key.pem 2048
  openssl rsa -in /path/to/private_key.pem -pubout -out /tmp/public_key.pem
  ```

  Opprett/oppdater navsync config-filen
  `~/.config/navsync/navsync.toml` (eventuelt bytt ut `~/.config/` med stien hvor
  konfigurasjon normalt blir lest fra på din maskin)
  ```toml
  # ~/.config/navsync/navsync.toml
  [netbox]
  url="http://127.0.0.1:8081"
  token="0123456789abcdef0123456789abcdef01234567"  # Denne nøkkelen blir godkjent av Netbox-instansen

  [nav]
  private_key_path="/path/to/private_key.pem"
  expiry_delta=3600
  issuer="navsync"
  https=false
  ```

  Opprett/oppdater `/tmp/jwt.conf` som vil bli plassert i NAV-web kontaineren
  i neste steg
  ```conf
  # /tmp/jwt.conf
  [navsync]
  aud=http://127.0.0.1:8080
  keytype=PEM
  key=/etc/nav/webfront/public_key.pem  # /tmp/public_key.pem blir plassert på denne stien i neste steg
  ```

  Kjør
  ```bash
  cd path/to/nav-repo
  docker compose cp /tmp/jwt.conf web:/etc/nav/webfront/jwt.conf
  docker compose cp /tmp/public_key.pem web:/etc/nav/webfront/public_key.pem
  docker compose restart web
  ```

  Til slutt,
  naviger til [/plugins/inventory/assets/add/](http://127.0.0.1:8081/plugins/inventory/assets/add/) i Netbox,
  og legg til en ny asset i Netbox med

  |Attributt   | Verdi          |
  |------------|----------------|
  |Tenant      | \<valgfritt>  |
  |Owner       | \<valgfritt>  |

  Finn id-en til den nye asseten, og naviger til [/plugins/inventory/assets/device/create/?asset_id=\<id>](http://127.0.0.1:8081/plugins/inventory/assets/device/create/)
  og legg til en ny device med

  |Attributt   | Verdi          |
  |------------|----------------|
  |Name        | 127.0.0.1:8080 |
  |Device Role | Verktøykasse   |
  |Status      | Active         |

- Under utvikling er det fordelaktig at Netbox- og NAV-instansene inneholder
  representative data. Netbox-instansen er allerede populert med en SQL-dump fra
  produksjon. NAV-instansen er ikke det.  Du bør skaffe deg en SQL-dump fra en
  NAV-instans i produksjon, og så bruke `navdump` for å populere den med data.

- Nå skal det gå an å åpne et REPL, importere `navsync.syncer.Syncer`, og
  instansiere en Syncer instans via `Syncer.from_settings()` som kan testes.

# Spesifikasjon
<!-- Basert på klistrelapp-notateter skrevet i en fei på slutten av dagen 25.07.2025. -->

## NAV.models.Netbox og NAV.models.NetboxEntity
VM i Netbox er en NAV-instans hvis:
- `service in {"cnaas", "nettadmin"}`
eller
- `contacts["owner"] in {"cnaas", "nettadmin"}`

(VM-et blir da kalt en verktøykasse)

Tenant til VM er tenant til dens NAV utstyr.

Owner til VM er owner til dens NAV utstyr

For NAV-instans som kjører i en verktøykasse Device (istedenfor verktøykasse VM) vil owner være owner av Device-asseten

URL på Device/VM er URL til NAV.

Merk at noen NAV-instanser er for flere kunder, e.g. CNaaS har i det faktiske tilfellet en egen NAV-instans med ip-enheter for mange ulike kunder. Her må man tenke seg
litt om hvordan man setter tenant og owner på utstyr, siden man ikke kan basere seg på tenanten av selve Devicen eller VM-et for NAV serveren i Netbox.

NAV-sync må holde oppdatert asset-status ("used", "stored") kontinuerlig
DVS:
- Hvis NAV ser en navbox som inneholder et serienummer som ikke fins i netbox: Lag ny asset
- Hvis serienummer finnes men asset er satt til "stored", sett den til "used" hvis `navbox.last_seen == None`,
- NAV-sync må også lage seg en liste over alle serienummer den har sett så den kan se om det er "used" assets som skal gå over til "stored"/"unused"
- Husk å oppdatere asset sin Tenant til tenanten av NAV-instansen hvor
  den befinner seg! (en asset kan meg bevisst bevege seg fra én NAV-instans med én tenant til en annen med en annen tenant, så man må holde denne oppdatert over tid)
- Device i Netbox kan ha forskjellig assets til forskjellig tid, så husk også å kontinuerlig endre Device `<->` asset relasjonen

## NAV.models.Location og NAV.models.Room
- Et NAV-rom som inneholder dataen `{"netbox_site": <addr>}` vil bli koblet under
  siten i Netbox med `<addr>` som addresse. NAV vil så instansiere et rom under
  den siten i Netbox. Alle NAV-serienummer i rommet vil gå til det
  netbox-rommet.
- En NAV-location med data `{"netbox_address": <addr>}` vil bli koblet til (eller
  under?) siten i Netbox med addresse `<addr>`. Alle NAV-rom under denne location
  vil bli underlagt denne siten.
- En NAV-location med data `{"addr" <addr>}` vil man forsøke å finne tilsvarende
  site i Netbox med den addressen. Ellers må man lage en ny site for denne
  location, gitt selvasgt at den finnes rom i NAV med >0 bokser for denne
  location.
- En NAV-location uten data må man også forsøke å enten finne i Netbox eller
  lage slik man kan fylle inn med assets/rom fra NAV.
