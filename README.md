# Fuel Insight

Aplikacja do analizy raportów paliwowych XLSX, tworzenia rankingu kierowców lub pojazdów oraz archiwizacji raportów lokalnie albo w PostgreSQL.

Repo jest przygotowane pod trzy tryby:

- desktop GUI na Windows: `app.py`,
- web UI na Debianie/VM: `fuel_insight_web.py` uruchamiany przez `systemd`,
- headless service na Debianie/VM: `fuel_insight_service.py` do recznego przetwarzania lub jako alternatywa.

## Jak działa analiza

- Do programu trafia raport XLSX z kolumnami m.in. `Numery rejestracyjne`, `Data`, `Ilość`, `Rodzaj towaru`, `Kilometry (telematyka)`.
- Numery rejestracyjne są normalizowane, więc np. `BI 228HA` i `BI228HA` oznaczają ten sam pojazd.
- Diesel/ON i Benzyna są sumowane jako paliwo do spalania. AdBlue jest raportowane osobno.
- Wiersze bez numeru rejestracyjnego są zachowywane jako `Brak numeru rejestracyjnego`, ale bez dystansu nie trafiają do rankingu.
- Dystans to różnica między największym i najmniejszym przebiegiem dla pojazdu w analizowanym okresie.
- Szacowane spalanie jest liczone jako `paliwo razem / dystans * 100`.

## Konfiguracja przez zmienne środowiskowe

Najważniejsze zmienne:

| Zmienna | Znaczenie | Przykład |
| --- | --- | --- |
| `FUEL_INSIGHT_DB_CONFIG` | plik JSON z połączeniem PostgreSQL | `/etc/fuel-insight/database.json` |
| `FUEL_INSIGHT_LOG_FILE` | plik logów aplikacji | `/var/log/fuel-insight/app.log` |
| `FUEL_INSIGHT_LOG_LEVEL` | poziom logowania | `INFO` |
| `FUEL_INSIGHT_WEB_HOST` | adres nasluchu panelu WWW | `0.0.0.0` |
| `FUEL_INSIGHT_WEB_PORT` | port panelu WWW | `8000` |
| `FUEL_INSIGHT_WEB_MAX_UPLOAD_MB` | limit uploadu XLSX w panelu | `100` |
| `FUEL_INSIGHT_DATA_DIR` | bazowy katalog danych | `/var/lib/fuel-insight` |
| `FUEL_INSIGHT_INPUT_DIR` | katalog wejściowy dla XLSX | `/var/lib/fuel-insight/inbox` |
| `FUEL_INSIGHT_OUTPUT_DIR` | katalog raportów wynikowych | `/var/lib/fuel-insight/outbox` |
| `FUEL_INSIGHT_PROCESSED_DIR` | katalog poprawnie przetworzonych plików źródłowych | `/var/lib/fuel-insight/processed` |
| `FUEL_INSIGHT_FAILED_DIR` | katalog plików z błędem | `/var/lib/fuel-insight/failed` |
| `FUEL_INSIGHT_DRIVER_MAPPING` | plik mapowania pojazd -> kierowca | `/etc/fuel-insight/kierowcy.json` |
| `FUEL_INSIGHT_REQUIRE_DB` | wymaga działającej bazy przy starcie | `1` |
| `FUEL_INSIGHT_POLL_INTERVAL` | interwał sprawdzania inboxa w sekundach | `60` |
| `FUEL_INSIGHT_MIN_DISTANCE` | minimalny dystans do rankingu | `100` |

Przykład env file jest w `deploy/fuel-insight.env.example`.

## Plik konfiguracji bazy

Aplikacja czyta połączenie z pliku wskazanego przez `FUEL_INSIGHT_DB_CONFIG`. Przykład jest w `config/database.example.json`:

```json
{
  "host": "127.0.0.1",
  "port": 5432,
  "database": "fuel_insight",
  "username": "fuel_user",
  "password": "change-me",
  "sslmode": "prefer",
  "connect_timeout": 5
}
```

Na Linuxie hasło jest zwykłym tekstem w tym pliku, więc ustaw prawa np. `0640` i właściciela/grupę tak, żeby czytał go tylko root oraz użytkownik usługi.

## Wdrożenie na Debianie przez systemd

Przykładowo zakładam instalację w `/opt/fuel-insight` oraz użytkownika systemowego `fuel-insight`.

```bash
sudo adduser --system --group --home /opt/fuel-insight fuel-insight
sudo install -d -o fuel-insight -g fuel-insight -m 0755 /opt/fuel-insight
sudo install -d -o root -g fuel-insight -m 0750 /etc/fuel-insight

sudo -u fuel-insight git clone <URL_TWOJEGO_REPO> /opt/fuel-insight
cd /opt/fuel-insight
sudo -u fuel-insight python3 -m venv .venv
sudo -u fuel-insight .venv/bin/pip install -r requirements.txt

sudo install -o root -g fuel-insight -m 0640 config/database.example.json /etc/fuel-insight/database.json
sudo install -o root -g root -m 0644 deploy/fuel-insight.env.example /etc/fuel-insight/fuel-insight.env
sudo install -o root -g root -m 0644 deploy/fuel-insight.service /etc/systemd/system/fuel-insight.service
```

Edytuj swoje dane:

```bash
sudo nano /etc/fuel-insight/database.json
sudo nano /etc/fuel-insight/fuel-insight.env
```

Uruchom usługę:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fuel-insight
sudo systemctl status fuel-insight
```

Panel WWW bedzie dostepny pod adresem:

```text
http://<IP_VM>:8000
```

Jesli nie widzisz strony z innego komputera, sprawdz nasluch i firewall:

```bash
sudo ss -ltnp | grep 8000
sudo ufw allow 8000/tcp
```

Logi:

```bash
journalctl -u fuel-insight -f
sudo tail -f /var/log/fuel-insight/app.log
```

Przetwarzanie pliku:

```bash
sudo cp raport.xlsx /var/lib/fuel-insight/inbox/
sudo chown fuel-insight:fuel-insight /var/lib/fuel-insight/inbox/raport.xlsx
```

Panel zapisuje archiwa do PostgreSQL albo lokalnego katalogu `/var/lib/fuel-insight/archiwum`. Eksporty XLSX/CSV trafiaja do `/var/lib/fuel-insight/outbox`.

## Reczne uruchomienie panelu WWW

Lokalny test panelu bez systemd:

```bash
FUEL_INSIGHT_DB_CONFIG=/etc/fuel-insight/database.json \
FUEL_INSIGHT_LOG_FILE=/var/log/fuel-insight/app.log \
/opt/fuel-insight/.venv/bin/python /opt/fuel-insight/fuel_insight_web.py --host 0.0.0.0 --port 8000
```

Panel przyjmuje upload XLSX, od razu pokazuje KPI, ranking, transakcje i archiwum. Eksport XLSX/CSV zapisuje pliki w outbox i pozwala je pobrac z panelu.

## Ręczne uruchomienie trybu headless

Jednorazowe przetworzenie całego inboxa:

```bash
FUEL_INSIGHT_DB_CONFIG=/etc/fuel-insight/database.json \
FUEL_INSIGHT_LOG_FILE=/var/log/fuel-insight/app.log \
/opt/fuel-insight/.venv/bin/python /opt/fuel-insight/fuel_insight_service.py
```

Przetworzenie pojedynczego pliku:

```bash
/opt/fuel-insight/.venv/bin/python /opt/fuel-insight/fuel_insight_service.py --file /path/to/raport.xlsx
```

Alternatywny tryb ciagly bez panelu WWW:

```bash
/opt/fuel-insight/.venv/bin/python /opt/fuel-insight/fuel_insight_service.py --watch
```

## Desktop GUI na Windows

Na Windows kliknij dwukrotnie:

`uruchom_aplikacje.bat`

Przy pierwszym uruchomieniu skrypt utworzy lokalne środowisko `.venv` i zainstaluje zależności z `requirements.txt`.

Alternatywnie:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

## Build EXE na Windows

```powershell
.\buduj_exe.bat
```

Po udanym buildzie gotowy plik będzie tutaj:

`wydanie\FuelInsight.exe`

## PostgreSQL

Aplikacja sama utworzy tabelę `fuel_insight_archives`, jeśli użytkownik bazy ma prawo tworzyć tabele w schemacie `public`.

Przykładowe minimum po stronie PostgreSQL:

```sql
CREATE DATABASE fuel_insight;
CREATE USER fuel_user WITH PASSWORD 'mocne_haslo';
GRANT CONNECT ON DATABASE fuel_insight TO fuel_user;
\c fuel_insight
GRANT USAGE, CREATE ON SCHEMA public TO fuel_user;
```

## Eksport

Eksport XLSX zawiera:

- arkusz `Podsumowanie` z rankingiem i wyróżnionym najgorszym wynikiem,
- arkusz `Transakcje` ze szczegółami wczytanego raportu,
- arkusz `Mapowanie kierowców`.

Dostępny jest także eksport CSV.
