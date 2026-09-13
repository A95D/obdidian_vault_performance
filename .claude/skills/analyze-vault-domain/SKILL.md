---
name: analyze-vault-domain
description: |
  Не вызывать напрямую. Анализ домена хранилища знаний выполняет агент
  vault-domain-analyzer (.claude/agents/vault-domain-analyzer.md) через
  Agent tool — его вызывает skill semantic-taxonomy (Фаза 2).

  📖 Справочник по схеме: ./references/REFERENCE.md
  📖 Примеры: ./references/examples.json
---

# Анализ домена хранилища знаний

Логика анализа и формат ответа находятся в `.claude/agents/vault-domain-analyzer.md`.

`references/REFERENCE.md` (схема полей анализа, формат выходного JSON) и
`references/examples.json` — общая документация, на неё ссылается агент
`vault-domain-analyzer` и skill `semantic-taxonomy`.