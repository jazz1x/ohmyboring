-- C-PORT4 fixture: split groups with a rows tie (mid / zeta / éclair at 2 each), an owner-written note,
-- a superseded row, a single-spelling subject. Loaded into a throwaway DB only.
DELETE FROM claim;
DELETE FROM document;
INSERT INTO document (source_path, author) VALUES
 ('n/a.md','owner'),('n/b.md','unknown'),('n/c.md','unknown'),('n/m1.md','unknown'),('n/m2.md','unknown'),
 ('n/z1.md','unknown'),('n/z2.md','unknown'),('n/e1.md','unknown'),('n/e2.md','unknown'),
 ('n/h1.md','unknown'),('n/h2.md','unknown'),('n/h3.md','unknown'),('n/s.md','unknown'),('n/o1.md','unknown'),('n/o2.md','unknown');
INSERT INTO claim (subject, predicate, value, source_path, valid_from, superseded_at) VALUES
 ('Foo Bar','p1','v1','n/a.md','2026-01-01',NULL),
 ('Foo Bar','p2','v2','n/a.md','2026-01-01',NULL),
 ('foo-bar','p1','v3','n/b.md','2026-01-02',NULL),
 ('foo_bar','p1','v4','n/c.md','2026-01-03',NULL),
 ('foo-bar','p3','v5','n/c.md','2026-01-03',NULL),
 ('mid','p','vm1','n/m1.md','2026-01-01',NULL),
 ('Mid','p','vm2','n/m2.md','2026-01-02',NULL),
 ('zeta','p','vz1','n/z1.md','2026-01-01',NULL),
 ('Zeta','p','vz2','n/z2.md','2026-01-02',NULL),
 ('éclair','p','ve1','n/e1.md','2026-01-01',NULL),
 ('Éclair','p','ve2','n/e2.md','2026-01-02',NULL),
 ('한 글','p','vh1','n/h1.md','2026-01-01',NULL),
 ('한-글','p','vh2','n/h2.md','2026-01-02',NULL),
 ('한_글','p','vh3','n/h3.md','2026-01-03',NULL),
 ('solo','p','vs1','n/s.md','2026-01-01',NULL),
 ('solo','q','vs2','n/s.md','2026-01-01',NULL),
 ('old','p','vo1','n/o1.md','2026-01-01','2026-02-01'),
 ('Old','p','vo2','n/o2.md','2026-01-02',NULL);
