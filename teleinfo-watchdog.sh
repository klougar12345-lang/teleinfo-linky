#!/bin/bash
# Chien de garde de la passerelle téléinfo — à exécuter SUR LE PI.
#
# Pourquoi : les topics de santé (sante-teleinfo) continuent de publier même
# quand la boucle de lecture TIC se fige. Ils ne prouvent donc RIEN sur la
# lecture réelle. Seul un topic de DONNÉES (teleinfo/BASE) fait foi.
#
# Règle de conception : ne JAMAIS redémarrer quand le problème n'est pas la
# lecture. Si le broker est injoignable, redémarrer teleinfo-mqtt est
# contre-productif : ce programme se connecte AVANT d'initialiser son tampon,
# donc chaque redémarrage le fait planter sans rien tamponner — on casserait
# le tampon local au moment précis où il est utile.
#
# Codes retour de mosquitto_sub, mesurés sur ce Pi :
#   0   une trame est arrivée
#   27  connecté au broker, mais aucune trame dans le délai → lecture TIC figée
#   autre (124, 5…) broker injoignable, identifiants refusés, réseau coupé
#
# Politique : DEUX contrôles consécutifs sans trame (10 min) avant d'agir, et
# abandon après trois redémarrages sans résultat — l'unité passe alors en
# échec, visible par `systemctl --failed`.

CONF=/etc/teleinfo/mqtt.conf
BROKER=192.168.0.78
TOPIC=teleinfo/BASE
DELAI=8
ETAT=/run/teleinfo-watchdog.echecs
MAX_ECHECS=2        # contrôles consécutifs sans trame avant d'agir
MAX_REDEMARRAGES=3  # trois redémarrages (N=2,3,4), abandon à N=5

[ -r "$CONF" ] || { echo "watchdog: $CONF illisible"; exit 1; }
UTILISATEUR=$(awk -F': *' '/^user:/{print $2; exit}' "$CONF")
MOTDEPASSE=$(awk -F': *' '/^password:/{print $2; exit}' "$CONF")
[ -n "$UTILISATEUR" ] && [ -n "$MOTDEPASSE" ] || { echo "watchdog: identifiants absents"; exit 1; }

lire_trame() {
    timeout $((DELAI + 4)) mosquitto_sub \
        -h "$BROKER" -p 1883 -u "$UTILISATEUR" -P "$MOTDEPASSE" \
        -t "$TOPIC" -C 1 -W "$DELAI" >/dev/null 2>&1
    return $?
}

# --- 1. Premier essai -------------------------------------------------------
lire_trame; RC=$?

if [ "$RC" -eq 0 ]; then
    rm -f "$ETAT"                  # trame reçue : tout va bien, compteurs à zéro
    exit 0
fi

case "$RC" in
    27) ;;                          # connecté, pas de trame : on continue l'analyse
    *)
        # Broker injoignable, réseau coupé, identifiants refusés : ce n'est PAS
        # la lecture TIC. On ne touche pas au service, qui tamponne peut-être.
        echo "watchdog: broker injoignable (rc=$RC) — aucun redémarrage, la lecture n'est pas en cause"
        exit 0
        ;;
esac

# --- 2. Second essai, le Linky peut être muet quelques secondes --------------
sleep 5
lire_trame; RC=$?
if [ "$RC" -eq 0 ]; then
    rm -f "$ETAT"
    exit 0
fi
if [ "$RC" -ne 27 ]; then
    echo "watchdog: broker injoignable au second essai (rc=$RC) — aucun redémarrage"
    exit 0
fi

# --- 3. Aucune trame alors que le broker répond : on compte ------------------
N=$(cat "$ETAT" 2>/dev/null)
case "$N" in ''|*[!0-9]*) N=0 ;; esac   # fichier absent, vide ou non numérique
N=$(( 10#$N + 1 ))
echo "$N" > "$ETAT"

if [ "$N" -lt "$MAX_ECHECS" ]; then
    exit 0                          # premier échec : on laisse une seconde chance
fi

if [ "$N" -ge $((MAX_ECHECS + MAX_REDEMARRAGES)) ]; then
    echo "watchdog: $N contrôles consécutifs sans trame malgré les redémarrages — la TIC est probablement muette (Linky hors tension ?). Intervention manuelle nécessaire."
    exit 1                          # unité en échec : visible dans systemctl --failed
fi

echo "watchdog: aucune trame sur $TOPIC alors que le broker répond ($N contrôles) — redémarrage de teleinfo-mqtt"
systemctl restart teleinfo-mqtt
sleep 10
if systemctl is-active --quiet teleinfo-mqtt; then
    echo "watchdog: teleinfo-mqtt redémarré, service actif"
else
    echo "watchdog: ÉCHEC du redémarrage, teleinfo-mqtt inactif"
    exit 1
fi
