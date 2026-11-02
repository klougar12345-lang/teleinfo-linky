# Rejeu du tampon vers Home Assistant

Le tampon SQLite du Pi ne vaut que s'il est relu. Ce dossier contient les deux
moitiés de la chaîne : celle qui publie les heures manquantes, et celle qui les
importe dans Home Assistant.

```
Pi (rejeu_pi.py, horaire)          MQTT              Corneille (rejeu-ha.py)
  lit le tampon SQLite
  publie teleinfo/rejeu      ───────────────────▶    valide la trame
                                                    importe la statistique
  attend l'acquittement      ◀───────────────────    publie teleinfo/rejeu/ack
  marque l'heure transmise
```

## Pourquoi deux étages

Le Pi n'a pas accès à Home Assistant et ne doit pas y écrire : il publie. Côté
Home Assistant, l'écriture passe par l'API websocket `recorder/import_statistics`,
qui exige un jeton et une connexion persistante — deux choses qui n'ont rien à
faire sur un Pi qui perd son WiFi.

## Pourquoi une statistique **externe**

Les mesures sont importées dans `teleinfo:energie_base`, une statistique de
source externe, et **jamais** dans `sensor.linky_index_stable`.

Ce dernier est compilé par le recorder de Home Assistant. Écrire dedans à la
main met deux auteurs sur la même série : Home Assistant recalcule alors des
cumuls à partir de valeurs qu'il n'a pas produites, ce qui a déjà provoqué des
remontées à zéro et un pic fictif de 15 MWh sur le tableau Énergie.

Une série, un auteur.

## Le protocole

- `teleinfo/rejeu` — le Pi publie une heure : `{"ts": …, "base": …}`.
- `teleinfo/rejeu/ack` — Home Assistant confirme `{"ts": …}` après avoir
  **vérifié dans la base** que l'écriture a bien eu lieu (`statistics_during_period`).

L'acquittement n'est pas une politesse : `import_statistics` répond « success »
avant l'écriture réelle. Sans relecture, on acquitterait des heures perdues.

Le Pi ne marque une heure comme transmise qu'après l'ack, et **ne purge jamais
au-delà du filigrane de rejeu** : une mesure non acquittée reste dans le tampon.

## Installation

### Sur le Pi

```bash
sudo install -m 755 rejeu_pi.py /usr/local/bin/
sudo install -m 644 rejeu_pi.service rejeu_pi.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rejeu_pi.timer
```

### Sur la machine Home Assistant

```bash
install -m 644 rejeu-ha.py ~/scripts/
install -m 644 rejeu-ha.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now rejeu-ha.service
```

`rejeu-ha.py` lit les identifiants MQTT dans `~/.mqtt-sante.conf` (format
`user:` / `password:`, en `600`) et le jeton Home Assistant dans les variables
d'environnement du conteneur `ha-linky`.

## Vérifier que ça marche

```bash
journalctl --user -u rejeu-ha -n 20        # « rejoué : N heures jusqu'à … »
```

Un `import refusé par Home Assistant` signifie que la cible d'import n'est pas
la bonne : Home Assistant n'accepte pas d'écriture dans une série qu'il gère
lui-même (`source: recorder`). C'est le symptôme à connaître.
