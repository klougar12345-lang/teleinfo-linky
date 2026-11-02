# Linky téléinfo → MQTT → Home Assistant

Passerelle complète pour lire la TIC d'un compteur Linky sur un Raspberry Pi et
publier les mesures dans Home Assistant, avec un tampon local qui survit aux
coupures réseau.

Écrit pour un Raspberry Pi 2 (ARMv7) avec un module **PiTInfo** sur l'UART, mais
rien n'est spécifique à cette carte : n'importe quel Pi avec `/dev/serial0` fait
l'affaire.

## Ce que ça fait

- Détecte automatiquement le mode TIC : **historique 1200 bds** ou **standard 9600 bds**.
- Publie `teleinfo/BASE`, `teleinfo/PAPP`, `teleinfo/IINST`, `teleinfo/ADCO`… sur MQTT.
- Publie la découverte Home Assistant (`homeassistant/sensor/linky_*/config`).
- **Tamponne chaque mesure dans une base SQLite locale**, indépendamment du réseau.
- Un chien de garde redémarre la lecture si les trames cessent alors que le broker répond.

## Installation

```bash
sudo install -m 755 teleinfo-mqtt.py        /usr/local/bin/
sudo install -m 755 teleinfo-watchdog.sh    /usr/local/bin/
sudo install -m 644 systemd/teleinfo-mqtt.service        /etc/systemd/system/
sudo install -m 644 systemd/teleinfo-watchdog.service    /etc/systemd/system/
sudo install -m 644 systemd/teleinfo-watchdog.timer      /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now teleinfo-mqtt.service teleinfo-watchdog.timer
```

Identifiants MQTT dans `/etc/teleinfo/mqtt.conf`, **en `600 root:root`** :

```
user: teleinfo
password: ...
```

UART à activer dans `/boot/firmware/config.txt` (`enable_uart=1`) et console
série désactivée sur le port, sinon la TIC est illisible.

## Les pièges, et pourquoi le code est écrit comme ça

### Le tampon doit être initialisé **avant** la connexion MQTT

C'est le point le plus important. Une passerelle qui démarre sans réseau doit
mesurer et enregistrer quand même. Si `init_tampon()` vient après `connect()`,
et que le programme quitte lorsque le broker est injoignable, alors **une
coupure réseau creuse un trou définitif** dans les données : le Pi ne tamponne
plus rien pendant la panne, et redémarrer le service aggrave les choses.

D'où `connect_async()` + `loop_start()` + `reconnect_delay_set()` : paho se
reconnecte tout seul, pendant que la lecture TIC et le tampon continuent.

Vérifié en conditions réelles : broker volontairement inexistant, le programme
démarre, lit la TIC et tamponne.

### Les topics de santé ne prouvent pas la lecture

Les capteurs `sante_*` (CPU, mémoire, charge) continuent de publier **même
quand la boucle de lecture TIC est figée**. Ils mesurent la machine, pas le
compteur. Seul un topic de **données** (`teleinfo/BASE`) prouve que la lecture
fonctionne — c'est ce que le chien de garde surveille.

### Jamais de `retain` sur les mesures

Un message retenu est republié par le broker à chaque abonnement, avec un
horodatage neuf : la mesure périmée redevient « fraîche ». Et un chien de garde
qui s'abonne à un topic retenu reçoit toujours quelque chose — il devient aveugle.

### Les codes retour de `mosquitto_sub` portent le diagnostic

| Code | Sens | Action |
|---|---|---|
| 0 | une trame est arrivée | rien |
| 27 | connecté, mais topic muet | lecture TIC figée → redémarrer, avec temporisation |
| 1 | hôte introuvable | **ne rien faire** |
| 124 | hôte injoignable | **ne rien faire** |
| 5 | identifiants refusés | **ne rien faire** |

Redémarrer sur autre chose que 27 revient à casser le tampon au moment où il
sert. Le chien de garde exige deux contrôles consécutifs sans trame, autorise
trois redémarrages, puis passe l'unité en échec pour que le problème soit
visible au lieu d'être masqué.

### L'horloge n'est pas fiable avant la synchro NTP

Pas d'horloge matérielle sur un Pi : au démarrage, l'horodatage des premières
mesures peut être faux de quelques secondes. Sans conséquence sur des mesures
espacées de cinq minutes, à garder en tête pour des analyses fines.

## Ce qui reste à faire

- **Rejeu du tampon automatique.** Le tampon n'a de valeur que s'il est relu.
  Ici, un service séparé sur une autre machine importe les heures manquantes
  dans les statistiques Home Assistant — il n'est pas inclus dans ce dépôt.
- **Un compte MQTT par client.** Aujourd'hui le même identifiant sert à la
  passerelle, au chien de garde et à plusieurs scripts : une fuite oblige à
  tout révoquer.
- **Sauvegarde de la carte SD.** Une carte morte emporte le tampon.

## Licence

MIT
