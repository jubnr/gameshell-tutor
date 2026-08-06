#!/usr/bin/env sh

_mission_init() (
  cd "$GSH_HOME"/'Pigeonnier' || return 1
  cat > 'message_01' <<'GSH_TUTOR_EOF'
Le marché aura lieu jeudi.
GSH_TUTOR_EOF
  cat > 'message_02' <<'GSH_TUTOR_EOF'
La récolte de pommes est bonne.
GSH_TUTOR_EOF
  cat > 'message_03' <<'GSH_TUTOR_EOF'
Rien à signaler au moulin.
GSH_TUTOR_EOF
  cat > 'message_04' <<'GSH_TUTOR_EOF'
Le pont est réparé.
GSH_TUTOR_EOF
  cat > 'message_05' <<'GSH_TUTOR_EOF'
URGENT : le dragon approche du village par la Foret !
GSH_TUTOR_EOF
  cat > 'message_06' <<'GSH_TUTOR_EOF'
Les moutons sont rentrés.
GSH_TUTOR_EOF
  cat > 'message_07' <<'GSH_TUTOR_EOF'
Le puits est de nouveau propre.
GSH_TUTOR_EOF
  cat > 'message_08' <<'GSH_TUTOR_EOF'
La foire est reportée.
GSH_TUTOR_EOF
  cat > 'message_09' <<'GSH_TUTOR_EOF'
Le forgeron cherche un apprenti.
GSH_TUTOR_EOF
  cat > 'message_10' <<'GSH_TUTOR_EOF'
Trois poules se sont échappées.
GSH_TUTOR_EOF
  cat > 'message_11' <<'GSH_TUTOR_EOF'
Le boulanger offre du pain.
GSH_TUTOR_EOF
  cat > 'message_12' <<'GSH_TUTOR_EOF'
La chandelle du phare est changée.
GSH_TUTOR_EOF
)

_mission_init
