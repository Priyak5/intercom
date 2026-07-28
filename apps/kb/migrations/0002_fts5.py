"""FTS5 virtual table + sync triggers for kb_article.

The virtual table mirrors title/body_text/category-name into an inverted index that
SQLite can MATCH against with bm25() ranking. Content-linking via `content='kb_article'`
keeps the FTS payload out of duplicate storage — FTS reads back from the source
column when a search hits.

Sync via three AFTER triggers (INSERT/UPDATE/DELETE) so any code path that writes
kb_article (service layer, shell, admin, tests) keeps the index in step — the
alternative (explicit service-layer sync) is easy to forget in future code paths.
"""

from django.db import migrations


CREATE_SQL = r"""
CREATE VIRTUAL TABLE IF NOT EXISTS kb_article_fts USING fts5(
  title, body_text, category,
  content='kb_article', content_rowid='rowid',
  tokenize='porter unicode61 remove_diacritics 1'
);

CREATE TRIGGER IF NOT EXISTS kb_article_ai AFTER INSERT ON kb_article BEGIN
  INSERT INTO kb_article_fts(rowid, title, body_text, category)
  VALUES (
    new.rowid,
    new.title,
    new.body_text,
    COALESCE((SELECT name FROM kb_category WHERE id = new.category_id), '')
  );
END;

CREATE TRIGGER IF NOT EXISTS kb_article_ad AFTER DELETE ON kb_article BEGIN
  INSERT INTO kb_article_fts(kb_article_fts, rowid, title, body_text, category)
  VALUES (
    'delete',
    old.rowid,
    old.title,
    old.body_text,
    COALESCE((SELECT name FROM kb_category WHERE id = old.category_id), '')
  );
END;

CREATE TRIGGER IF NOT EXISTS kb_article_au AFTER UPDATE ON kb_article BEGIN
  INSERT INTO kb_article_fts(kb_article_fts, rowid, title, body_text, category)
  VALUES (
    'delete',
    old.rowid,
    old.title,
    old.body_text,
    COALESCE((SELECT name FROM kb_category WHERE id = old.category_id), '')
  );
  INSERT INTO kb_article_fts(rowid, title, body_text, category)
  VALUES (
    new.rowid,
    new.title,
    new.body_text,
    COALESCE((SELECT name FROM kb_category WHERE id = new.category_id), '')
  );
END;
"""

DROP_SQL = r"""
DROP TRIGGER IF EXISTS kb_article_au;
DROP TRIGGER IF EXISTS kb_article_ad;
DROP TRIGGER IF EXISTS kb_article_ai;
DROP TABLE IF EXISTS kb_article_fts;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("kb", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_SQL, reverse_sql=DROP_SQL),
    ]
