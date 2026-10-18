# توثيق حالة GitHub — 2026-10-18

**الغرض:** تثبيت حالة المستودع على GitHub كما قرأتها الأدوات فعليًا، حتى لا تُعاد
قراءتها من ذاكرة جلسة سابقة أو من لقطة «الـPRs المفتوحة» وحدها.

**القاعدة المُطبَّقة هنا هي قاعدة المشروع نفسه:** رقم بلا الأمر الذي أنتجه إعلان وليس
دليلًا. لذلك كل سطر أدناه مقرون بالأمر ونصّ مُخرَجه.

| الحقل | القيمة |
|---|---|
| وقت التحقق (UTC) | `2026-10-18T07:55:13Z` |
| رأس `origin/main` | `08ad433052c94576abd4e5e8cfcd566d8eb1bb12` |
| فرع العمل في ذلك الـsandbox | `arena/3aa6d80d-open-source` (مطابق لرأس `main`) |
| نظافة شجرة العمل | `git status --porcelain` → 0 سطر |

## الخلاصة في ثلاث نقاط

1. **#46 و#47 موجودان ومدموجان في `main`.** غيابهما عن قائمة المفتوحين سببه الدمج،
   لا عدم الوجود.
2. **`.github/workflows/codacy.yml` محذوف فعلًا** من `main` (حذفه PR #47). الملف غير
   موجود محليًا وعلى `origin` معًا.
3. **لا يوجد عمل متبقٍّ من #45/#46/#47 يستوجب فرعًا جديدًا أو PR جديدًا.** المتبقّي
   فعلًا هو الـPRs الأربعة المفتوحة.

## 1. الـPRs المفتوحة (4 بالضبط)

```
$ gh pr list --state open --limit 60 --json number,title,headRefName,state
32  OPEN  arena/69f5bce4-open-source  Add local research paper and skill import workflows
31  OPEN  arena/1fc5223a-open-source  ADR-0019: ميزانية بالتوكنز، متجهات مخزَّنة، وبوابة A/B مقاسة
21  OPEN  arena/d9c3a101-open-source  fix(web): pin PostCSS and deduplicate snapshot polling
7   OPEN  arena/bb2613c4-open-source  feat(metrics): persist a run-outcome ledger for every execution
```

هذه اللقطة صحيحة، وهي **كل** ما تُثبته. «ليس مفتوحًا» ≠ «غير موجود».

## 2. PR #46 — موجود ومدموج

```
$ gh pr view 46 --json number,state,title,headRefOid,mergedAt,mergeCommit
number      46
state       MERGED
headRefOid  e81cf9833eaf2fba943d561ab22010627bef3732
mergedAt    2026-10-17T17:09:57Z
mergeCommit 12725fb3a0e452e26e1bb4bc04965f21ea9f12d2
```

الرأس `e81cf98` **وصل إلى `origin`** — عكس ما قيل:

```
$ git ls-remote origin refs/heads/arena/b269200d-open-source
e81cf9833eaf2fba943d561ab22010627bef3732  refs/heads/arena/b269200d-open-source

$ gh api repos/sayedelazameydesign-crypto/open_source-/commits/e81cf9833eaf2fba943d561ab22010627bef3732
sha      e81cf9833eaf2fba943d561ab22010627bef3732
date     2026-10-17T17:00:48Z
subject  test(docs): fail on dangling ADR references, broken links, and typed counts
```

ملاحظة تشخيصية مهمة: `git cat-file -t e81cf98` يفشل في هذا الـsandbox
(`fatal: Not a valid object name`) لأن الاستنساخ **ضحل**:
`git rev-parse --is-shallow-repository` → `true` وملف `.git/shallow` فيه سطر واحد.
فشل الأمر محليًا ليس دليلًا على غياب الـcommit من `origin`.

## 3. PR #47 — موجود ومدموج

```
$ gh pr view 47 --json number,state,title,headRefOid,mergedAt,mergeCommit
number      47
state       MERGED
headRefOid  6db20ead2abae73a70b52e5717dbf68e8b537e38
mergedAt    2026-10-18T07:01:31Z
mergeCommit 4ca026a5bf8dd761a387fafa15e6632209368d28
title       docs: SECURITY.md, declare CELA.PRO standalone, drop the dead Codacy check
```

## 4. `codacy.yml` محذوف

```
$ ls -la .github/workflows/
ci.yml  codeql.yml  eval-real-model.yml  launcher-ci.yml  web-ci.yml

$ gh api repos/sayedelazameydesign-crypto/open_source-/contents/.github/workflows/codacy.yml?ref=main
{"message":"Not Found","status":"404"}

$ gh api repos/sayedelazameydesign-crypto/open_source-/pulls/47/files
removed   .github/workflows/codacy.yml
modified  CONTRIBUTING.md
modified  README.md
added     SECURITY.md
modified  docs/ROADMAP.md
```

ثلاثة مصادر مستقلة (شجرة العمل، API المحتوى على `main`، قائمة ملفات الـPR) تقول
الشيء نفسه: الملف محذوف.

## 5. الدمج داخل `main` الحالية

```
$ gh api "repos/sayedelazameydesign-crypto/open_source-/commits?sha=main&per_page=12"
08ad433  fix(skills): PostgresSkillStore recorded no versions, so rollback was a silent no-op (#48)
4ca026a  docs: SECURITY.md, declare CELA.PRO standalone, drop the dead Codacy check (#47)
12725fb  Merge pull request #46 from sayedelazameydesign-crypto/arena/b269200d-open-source
e81cf98  test(docs): fail on dangling ADR references, broken links, and typed counts
a9e60a0  docs(evidence): generate the test count instead of typing it
1b0ef7f  docs(adr): write the ADR-0006 that ROADMAP.md has been citing
554f3b9  Merge pull request #45 from sayedelazameydesign-crypto/arena/b269200d-open-source

$ gh api ".../compare/12725fb3a0e452e26e1bb4bc04965f21ea9f12d2...main"
status=ahead  ahead_by=2  behind_by=0

$ gh api ".../compare/4ca026a5bf8dd761a387fafa15e6632209368d28...main"
status=ahead  ahead_by=1  behind_by=0
```

`behind_by=0` هو السطر الحاسم: لا يوجد commit في `main` مفقود من سلفَي #46 و#47.

## 6. ما لم يُتحقَّق منه (غير مُتحقَّق)

- `UNPUSHED_WORK.md` غير موجود في هذا الـsandbox: `find / -name UNPUSHED_WORK.md`
  لم يُرجع شيئًا. أي تعديل وُصف على ذلك الملف حدث في sandbox آخر.
- `g1a-src.tar.gz` و`fastapi-cloud-bundle.tar.gz` غير موجودين على القرص، لذا بصمتا
  `sha256` المذكورتان في مراسلات سابقة **لم تُتحقَّقا**.
- حالة الفحوصات وCodacy على GitHub لم تُقرأ هنا؛ تُقرأ من واجهة GitHub.

## 7. ما يجب ألّا يتكرر في الجلسة القادمة

- لا تدفع إلى `arena/b269200d-open-source`؛ عملها مدموج.
- لا تفتح PR جديدًا بمحتوى #45/#46/#47؛ سيكون مكررًا.
- لا تستنتج وجود/عدم وجود PR أو فرع من `git` المحلي وحده في استنساخ ضحل — استخدم
  `gh api` أو `git ls-remote`.
- لا تُصدِّر رقمًا (عدد PRs، عدد اختبارات، حالة فحص) دون الأمر الذي أنتجه.
