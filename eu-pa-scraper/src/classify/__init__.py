# EN: Site classification package: nature + entity type, as a cascade of optional stages (0
# hostname, 2 Wikidata, 3 homepage, 4 local LLM).
"""
Classificazione dei siti: natura (pubblico/privato/...) + tipo di ente
(comune, regione, polizia, governo, scuola, università, sanità...).

Cascata di stadi opzionali, TUTTI disattivati per default (vedi
config_classify.yaml e classify_sites.py):
  stadio 0  hostname/suffisso (regole, zero rete)
  stadio 2  Wikidata (sito ufficiale P856 -> classi dell'entità)
  stadio 3  homepage -> pacchetto di evidenza -> regole pesate multilingue
  stadio 4  LLM locale (Ollama) sul pacchetto di evidenza, solo sul residuo
Gli stadi 1 (join con i registri ufficiali) e 5 (revisione umana) non sono
implementati: 'da_rivedere' nell'output marca i casi da guardare a mano.
"""
