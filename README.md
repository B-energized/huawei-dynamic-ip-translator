# Huawei Dynamic IP Translator

Deze aparte Home Assistant-integratie vindt een Huawei-omvormer, Smart Dongle of EMMA terug op basis van zijn MAC-adres. Huawei Solar gebruikt een vast lokaal adres en kan via HACS zijn gewone updates blijven krijgen.

## Installeren vanaf je gsm

Vereist Home Assistant 2026.8 of nieuwer.

<!-- ha-marketplace:exclude-start -->

1. Open [Huawei Dynamic IP Translator in Home Assistant](https://my.home-assistant.io/redirect/hacs_repository/?owner=B-energized&repository=huawei-dynamic-ip-translator&category=integration). Kies indien nodig de Home Assistant-installatie van de klant.
2. Werkt de link niet? Ga vanaf Home Assistant 2026.11 naar **Instellingen → Marketplace → Toevoegen via link**. Gebruik op oudere ondersteunde versies **HACS → ⋮ → Aangepaste repositories**. Voeg `https://github.com/B-energized/huawei-dynamic-ip-translator` toe als **Integratie**.
3. Open **Huawei Dynamic IP Translator**, installeer de integratie en herstart Home Assistant.

<!-- ha-marketplace:exclude-end -->

## Eenmalig instellen

1. Ga naar **Instellingen → Apparaten en diensten → Integratie toevoegen → Huawei Dynamic IP Translator**.
2. Vul het **MAC-adres van het netwerktoestel waarmee Huawei Solar verbindt** in. Neem de bestaande Huawei Modbus TCP-poort en de eerste Modbus-unit-ID over uit Huawei Solar. De voorbeeldwaarden zijn `502` en `1`. Laat de lokale poort op `15020` staan, of kies per extra bridge een vrije poort.
3. Open bij je bestaande Huawei Solar-integratie **⋮ → Opnieuw configureren**. Vul `127.0.0.1` in als host en `15020` als poort, of gebruik de gekozen lokale poort. Behoud de overige Huawei Solar-instellingen. Laat Huawei online tijdens deze eerste verbinding, zodat de bridge het serienummer kan opslaan.

## Herstel na een adreswijziging

Wanneer Huawei Solar na uitval opnieuw verbinding maakt, zoekt de bridge naar het ingestelde MAC-adres in de DHCP-gegevens die Home Assistant kent. Als het bekende adres niet meer werkt, volgt een ARP-scan. De bridge vergelijkt op elke nieuwe verbinding het serienummer. Pas na het antwoord van het juiste toestel begint ze de Modbus-verbinding door te sturen. Scans zijn beperkt tot één poging per minuut per bridge.

De bridge luistert alleen op `127.0.0.1`. Huawei Solar en de bridge moeten in dezelfde Home Assistant-installatie draaien. De ARP-scan vereist Home Assistant OS of Linux met toegang tot het netwerk van de klant. Voor Home Assistant Container op Linux gebruik je hostnetwerkmodus. ARP werkt op hetzelfde lokale IPv4-netwerk, op ingeschakelde Home Assistant-netwerkinterfaces en op subnetten met maximaal 1024 adressen. Over een router of VLAN heen is herstel afhankelijk van de DHCP-informatie die Home Assistant kan zien. Het Huawei-toestel moet het serienummer via Modbus-register `30015` aanbieden op de ingestelde unit-ID.

## Controleren

Controleer of de bestaande Huawei Solar-sensoren na de eerste configuratie opnieuw data krijgen. Laat het toestel in een testinstallatie vervolgens een ander IP verkrijgen met hetzelfde MAC-adres. Controleer of dezelfde sensoren vanzelf opnieuw data krijgen en of Huawei Solar op `127.0.0.1` staat. Bij een fout kun je in Huawei Solar via **Opnieuw configureren** terugkeren naar het rechtstreekse adres en de oorspronkelijke poort.

Draai de simulatie met Python 3.12 of nieuwer via `python -B -m unittest discover -s tests -v`.
