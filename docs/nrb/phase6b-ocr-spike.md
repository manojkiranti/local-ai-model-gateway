# Phase 6B — OCR spike: can Docling read what Preeti conversion cannot?

Experimental spike. **No production change**: native-2 is untouched, no threshold moved, no conversion or OCR is wired into any pipeline, and no frozen artifact was modified. Every number below is **structural** — codepoint counts and runtime. Whether the Nepali is *correct* is a competent reader's call, exactly as in `phase6b-routing-holdout-manual-review.md`.

**Exactly what was tested** — one configuration, no sweep:

| layer | this run |
| --- | --- |
| orchestration | docling OCR stage (`PdfPipelineOptions.do_ocr=True`, `force_full_page_ocr=True`) |
| engine | RapidOCR 3.9.2 |
| inference backend | **`torch`** |
| detection / recognition | `ch_PP-OCRv4_det_mobile` / **`devanagari_PP-OCRv4_rec_mobile`** |
| OCR render scale | docling default `3.0` (not swept) |
| native comparison | `pypdf`, the same call `app/nrb/extraction.py` makes |

**PP-OCRv5 Devanagari was NOT tested here, and the reason is structural.** Docling's `_resolve_rapidocr` sends the `torch` backend down a PP-OCRv4-only branch — v5 recognition weights are published for `onnxruntime`, `openvino` and `paddle`, not for torch. This venv has torch but **no `onnxruntime`**, so v4 was the only Devanagari recogniser reachable without adding a package. Choosing torch was what kept this spike to a weights fetch instead of a dependency change; the cost is that the v4-vs-v5 question stays open here.

14 pages, 35s wall clock.

## 1. Why these pages — all 56 queue members accounted for

The `>=0.80` queue is not one population. `pdffonts` + `pdfimages` split it along the conversion outcome. Every one of the 56 frozen queue members appears in exactly one row, and the row totals reproduce the queue's own 36/16/4:

| page provenance | recovered | partial | unresolved | n |
| --- | ---: | ---: | ---: | ---: |
| PDF, embeds ≥1 recognised legacy Nepali font | 32 | 12 | 0 | 44 |
| PDF, embedded fonts whose names the producer stripped (`CIDFont+F1…F6`) | 1 | 0 | 0 | 1 |
| **PDF, NO embedded font — scan + hidden OCR text layer** | **0** | **4** | **4** | **8** |
| spreadsheet (`.xlsx` — has no PDF font layer to inspect) | 3 | 0 | 0 | 3 |
| **total** | **36** | **16** | **4** | **56** |

Two rows exist only so the arithmetic is honest. `7820b1f49fc1` embeds six subset fonts renamed `CIDFont+F1…F6` by its producer, so its provenance is **undetermined by name**, not "not legacy"; it converted cleanly and behaves like the 44. The 3 spreadsheets have no PDF font layer at all, so the question is not askable of them — they are not evidence for or against the split. Excluding them leaves 53 PDFs, which is the number an earlier draft of this report showed as the whole queue; that was an under-stated denominator, not a different measurement.

The 8 scan-backed blobs carry **no Preeti at all** — their text layer is legacy Latin-alphabet scanner OCR. A glyph mapping cannot be right there because the file holds no glyph mapping, only pixels.

### Whose failure this is

**Not native-2's.** Its contract in `app/nrb/routing.py` is *"did extraction produce trustworthy text"*; it judges text signals, never opens a font table, imports nothing from `legacy_font`, and must run where npttf2utf was never installed. Scanner-OCR noise is not English, not Unicode, and does look glyph-mapped — so `suspicious`/`legacy_font_suspected` is the **correct** call. That text really is untrustworthy.

The gap is downstream of the classifier, in something not yet built: reading `unit_legacy_ratio >= 0.80` as *eligible for npttf2utf* assumes the suspicious text is a glyph mapping of an embedded legacy font, and 8 queue members break that assumption. This is therefore a **conversion-routing / font-provenance** finding. Its fix is a precondition on the conversion router that §14.7 and §15.9 recommend building — **not** a classifier change, **not** `native-3`, and **not** a threshold move.

## 2. Results

| blob | page | group | native dev | **OCR dev** | OCR dev ratio | latin ratio | pre-base misordered | s |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `3d2eca8b9f95` | 1 | scan-backed queue | 0 | **1347** | 0.791 | 0.185 | 1 | 20.19 |
| `da8024b7616b` | 1 | scan-backed queue | 0 | **1567** | 0.8863 | 0.095 | 1 | 1.17 |
| `796bb59c3443` | 1 | scan-backed queue | 0 | **590** | 0.8806 | 0.1015 | 2 | 0.9 |
| `70b0d415dcf3` | 1 | scan-backed queue | 0 | **1650** | 0.9111 | 0.0729 | 7 | 1.18 |
| `2e65dadfffa3` | 1 | scan-backed queue | 0 | **667** | 0.8065 | 0.1669 | 2 | 0.82 |
| `360eaafd44bd` | 1 | scan-backed queue | 0 | **1198** | 0.7794 | 0.1835 | 0 | 1.1 |
| `a17fa322b81a` | 1 | scan-backed queue | 0 | **1499** | 0.7952 | 0.1756 | 2 | 1.15 |
| `8e8467f74f84` | 1 | scan-backed queue | 0 | **1429** | 0.7826 | 0.1911 | 1 | 1.09 |
| `438c55304da5` | 1 | needs_ocr | 0 | **784** | 0.716 | 0.0694 | 0 | 0.88 |
| `c298efaf1f16` | 1 | needs_ocr | 0 | **1234** | 0.6245 | 0.2647 | 0 | 0.98 |
| `276b2eb62802` | 1 | needs_ocr | 0 | **364** | 0.4505 | 0.2054 | 0 | 0.77 |
| `1a9b6321aa61` | 1 | font-embedded control | 0 | **540** | 0.9872 | 0.0 | 1 | 0.55 |
| `d1c99f3cf34d` | 1 | font-embedded control | 0 | **385** | 0.8594 | 0.1183 | 0 | 0.58 |
| `268bcfe86d03` | 1 | font-embedded control | 0 | **486** | 0.7265 | 0.2347 | 0 | 0.65 |

Runtime: median **1.0s/page**, range 0.6–20.2s, on CPU with the torch backend. The first page carries model load and `torch.compile` warmup.

**`pre-base misordered`** counts vowel signs that open a cluster — PP-OCR emits glyphs in *visual* order, so `वि` comes back as `िव`. Read it as a floor, not a count: the regex needs a space before the sign, and this output has almost no spaces, so nearly every real occurrence is invisible to it. The excerpts in §4 show the true rate.

## 3. Is the recovered text well-formed Nepali? No — and this is the finding

Recovering Devanagari codepoints is not the same as recovering Nepali. Two structural signals, neither of them semantic, measured against the converter's own output on the same queue — the only Nepali here already judged structurally plausible:

| text | halant per Devanagari char | mean Devanagari word length |
| --- | ---: | ---: |
| **reference** — npttf2utf on 56 font-embedded queue docs | **0.0982** | **5.7** |
| OCR — `3d2eca8b9f95` (scan-backed queue) | 0.0082 | 27.0 |
| OCR — `da8024b7616b` (scan-backed queue) | 0.0070 | 43.0 |
| OCR — `796bb59c3443` (scan-backed queue) | 0.0068 | 17.5 |
| OCR — `70b0d415dcf3` (scan-backed queue) | 0.0030 | 30.8 |
| OCR — `2e65dadfffa3` (scan-backed queue) | 0.0030 | 27.5 |
| OCR — `360eaafd44bd` (scan-backed queue) | 0.0008 | 25.8 |
| OCR — `a17fa322b81a` (scan-backed queue) | 0.0027 | 22.9 |
| OCR — `8e8467f74f84` (scan-backed queue) | 0.0056 | 47.2 |
| OCR — `438c55304da5` (needs_ocr) | 0.0064 | 19.7 |
| OCR — `c298efaf1f16` (needs_ocr) | 0.0016 | 11.2 |
| OCR — `276b2eb62802` (needs_ocr) | 0.0110 | 22.8 |
| OCR — `1a9b6321aa61` (font-embedded control) | 0.0000 | 28.7 |
| OCR — `d1c99f3cf34d` (font-embedded control) | 0.0026 | 36.0 |
| OCR — `268bcfe86d03` (font-embedded control) | 0.0000 | 29.2 |

Nepali is conjunct-heavy and the virama binds the conjuncts. The converter's output carries one about every ten Devanagari characters; this OCR carries one every ~200 — roughly **twenty times fewer** — and its mean word runs 4-8× too long because word boundaries are gone. Concretely, on a control the existing path already handles:

```
conversion : कारवाही फुकुवा भएका वित्त कम्पनीहरुको विवर०ा
OCR        : कारवाहीफुकुवाभएकािवतकमपनीहरकोिववरण
```
Same page, same words. The OCR loses every space, drops `वित्त`→`िवत` and `कम्पनी`→`कमपनी`, and reorders `वि`→`िव`. **A retrieval index built on this would not match a correctly typed Nepali query**, and no amount of embedding quality fixes a token that is one 27-character run. That is why the recommendation below is narrow.

## 4. Page by page — source → existing pipeline → OCR

### `3d2eca8b9f95` p.1 — scan-backed queue

- unresolved; 300 dpi scan; forex 2018
- blob: `3d2eca8b9f951ecda27129f3fc7e90c503eb1e84802797d6e9833fdae6d39e39.pdf`
- rendered source: `docs/nrb/holdout-pages/3d2eca8b9f95-p001.jpg`

**Existing text layer (pypdf)** — 0 Devanagari chars:

> tqrq w +6 a-ffi i$i?ii!i?T t{tqft frfu{q aFrerFFT f{qr{r {rdF{ (1) (R) (v) (s) ({) /C \ (1) (10) qTftrq ffi rrrrfrq Trrqr flur k{ Tfr q*w vq*ft qT{r Rqft hhqq tffia q-il'l E{, tott +1 ssT lo<r. d M BTRr+ri rfrq q-ft qs *+-qrs gqmaq{ qrq rA61 4tftr$q dq-{€A tarftFrqrffi qrccr qf rrft ,Trrf,terf, il+ aqr ffiq veTw€qrd }il-cfrq {ffqT €q fu{ {d qzreuTr qRr|+tA {qfrrd rrffi qr+arffi qrftr fr qErfl TsTqrq rM g r q-q +fi-Er …

**Existing legacy conversion** (from the review pack):

| in | out | disposition |
| --- | --- | --- |
| `t{tqft frfu{q aFrerFFT f{qr{r` | र्ततत्रात ाचार्गत्र बँचभचँत् र्त्रार्चच | rejected |
| `qTftrq ffi rrrrfrq Trrqr flur k{ Tfr q*w vq*ft` | त्रततचत्र ााष् चचचचाचत्र त्चचत्रच ागिच र्प तच त्र८ध खत्र८ात | rejected |
| `Rqft hhqq tffia q-il'l E{, tott +1 ssT lo<r. d M BTRr+ri rfrq q-ft qs ` | च्त्रात जजत्रत्र तााष्ब त्र(ष्िुि भर््, तयतत ंज्ञ ककत् यि?च। म : द्यत् | rejected |
| `rA61 4tftr$q dq-{€A tarftFrqrffi qrccr qf rrft ,Trrf,terf, il+ aqr ffi` | चब्टज्ञ द्धतातच४त्र मत्रर्(€ब् तबचातँचत्रचााष् त्रचअअच त्रा चचात ,त्चच | rejected |

**Docling OCR** — 1347 Devanagari chars:

> e al viht Hjale hle e? िवदेशीिविनमयथवरथापनिवभाग Uest lohe Mh Ery lnre Mih hpIk pePere beolnjie सूचना वदेशीिविनमयतनयिमतगने ऐन,रणरकोदफापoख.लेिदएकोआंधकारपयोगगरीयसबैंकबाटइजाजतपनपात गरेकावािणजयबैंकहरलेदेहायकाशतहरकोपालनागनेगरीभारतिरथतबैंकतथािवतीयसंरशाहरबाटभारतीयमुदामाऋण लनसवनेयवरथागिरएकोलेसमबिधतसबैकोजानकारीकोलािंगयोसूचनापकाशनगिरएकोछ शतहर - यसबैकबाटइजाजतपघपातकवगंकाबैंकहरलेनवीकरणीयउजिजलिव्धुत,सौयीउजिरवायुउजिउलादन तथापसारणलाइ …

### `da8024b7616b` p.1 — scan-backed queue

- unresolved; 300 dpi scan; forex 2019
- blob: `da8024b7616ba6a0513a99b585060ee7829905eb867e73d913b6086692ede980.pdf`
- rendered source: `docs/nrb/holdout-pages/da8024b7616b-p001.jpg`

**Existing text layer (pypdf)** — 0 Devanagari chars:

> iqrq {rE +€ ffiq +rqfqq kt{ft P{fu{q aFrerFFT f+n{r (1) (R) (Y) ({) \\/ (\9) (1) (10) qg trn ffiq ffi qRr+d Htqft 1nrqr apr k{ vfr aFreil Tqfrft qq{r Rqft fuhqq ttrqfua qill tc, tolt +1 Esl loq. A M BTRI+TT Tfrq rrft qq +{-qrd Fq[qilT{ qrq qt{r qq tff ffi ar{l-6-{A kIT+T flffi cm{r q-i ,Tft htst.*+ aqr ffiq €€Tr zTT q-q RrTr6{-crcqk{fli Rqt gcrqr ruq kc e-d_q+en rrMfA rr-qtrro sffi qrf,+Ttal qrfu * qrffic-r qffir wFl …

**Existing legacy conversion** (from the review pack):

| in | out | disposition |
| --- | --- | --- |
| `kt{ft P{fu{q aFrerFFT f+n{r` | पर्तात र्एार्गत्र बँचभचँत् ांर्लच | rejected |
| `qg trn ffiq ffi qRr+d Htqft 1nrqr apr k{ vfr` | त्रन तचल ााष्त्र ााष् त्रच्चंम ज्तत्रात ज्ञलचत्रच बउच र्प खाच | rejected |
| `Rqft fuhqq ttrqfua qill tc, tolt +1 Esl loq. A M BTRI+TT Tfrq rrft qq ` | च्त्रात ागजत्रत्र ततचत्रागब त्रष्िि तअ, तयति ंज्ञ भ्कि यित्र। ब् : द्य | rejected |
| `qt{r qq tff ffi ar{l-6-{A kIT+T flffi cm{r q-i ,Tft htst.*+ aqr ffiq €` | त्रर्तच त्रत्र ताा ााष् बर्च(िटर्(ब् पक्ष्त्तं् ाािाष् mर्अच त्र(ष् ,त | rejected |

**Docling OCR** — 1567 Devanagari chars:

> e Rol siht hhler hlye? वदेशीिविनमययवरथापनिवभाग Eh Erl nk Wiph UoP eh peeo hpe Pe Be यवरथासमबधीसूचना वदेशीिविनमयतनयिमतगने ऐन,र॰परकोदफापoख.लेिदएकोओिधकारपयोगगरीयसबैंकबाटइजाजतपघपात गरेकालघुिवचिवतीयसंरथाहकलेदेहायकाशतहरकोपालनागनेगरीिवदेशीबैकतथािवतीयसंरथावाअयसंरथाहरबाट पिरवयीिवदेशीमुदामाऋणिलनसवनेयवरथागिरएकोलेसमबिधतसबैकोजानकारीकोलिंगयोसावजिनकसूचना पकाशनगिरएकोछ शतहर - यसबैकबाटइजाजतपऋपातघवगकालघुवतिवतीयसंरशाहहलेिवदेशीमुदाआजनगने …

### `796bb59c3443` p.1 — scan-backed queue

- unresolved; 150 dpi scan; notice 2019
- blob: `796bb59c34434e9e6bc5480e30a821cf11e333a93a709e075f26bbd52d5005b7.pdf`
- rendered source: `docs/nrb/holdout-pages/796bb59c3443-p001.jpg`

**Existing text layer (pypdf)** — 0 Devanagari chars:

> fr{q{ ffil{.r qrfrr q+c q-il,sil-el{fr} Ft|{r I kdrefTrR w-;tfr r ql-{ c: o\e1-{lolqq WIFRT q: oel-{?olqe' Site: www nrb org np Enrail: nrbsid(frrrb org np farfqro ffi 1ou{/oR/ol qq ++,qre ffi qoev/lR/oR eJ @T€il@1 ."ftft14' sx" qtt+mr aeil ffit {rm[5afl. rari{ra TfiT 3]'.ilsR zl-q o.[qi-{q qzF[, BTfh"of, eTt"{RT dsTT ,Td qqrfld[{TT Tr[ful-{ I-i 6.rtf6-r qrfrr Rsq{-ft frdqT B{rFl]-{ @qr fu{o tn gt 3il-g6t k"l.{-fr tt …

**Existing legacy conversion** (from the review pack):

| in | out | disposition |
| --- | --- | --- |
| `fr{q{ ffil{.r qrfrr q+c q-il,sil-el{fr} Ft\|{r I` | ार्चर्त्र ााषर््ि।च त्रचाचच त्रंअ त्र(ष्,िकष्(िर्भिाचै ँर्त्रच क्ष् | rejected |
| `Site: www nrb org np` |  | kept_english |
| `Enrail: nrbsid(frrrb org np` | भ्लचबष्सि् लचदकष्म९ाचचचद यचन लउ | rejected |
| `qq ++,qre ffi qoev/lR/oR eJ @T€il@1 ."ftft14' sx" qtt+mr aeil` | त्रत्र ं,त्रचभ ााष् त्रयभखरच्रियच् भव् २त्€ष्२िज्ञ ।ूातातज्ञद्धु कहू क | rejected |

**Docling OCR** — 590 Devanagari chars:

> फोन न O-yoदद पयाiaस न Ou-yroe Site: www nrb org np Email: nrbsidanrb org np पकिशत िमित २०७xI०श०२ बोलपघरवीकृतकालिंगछनौटगनेआशयकोसूचना यसबैंकबाटिमित२॰णवशाणशकोरिषट्यरतरको.दैनकपऋपिकामातथा बैंककोवेभसाइटमापकिशतर सूचनाअनुसारयसकायिलयकोकायिलयभवन Io Piela Piprlia गाडीववाटरहकमारंगरोगनगनेकायकालािगसलबदी बोलपघआहवानगिरएकोमारीतपूवेकपेशहुनआएकािसलवदीबोलपनहरमधयेधी भगवतीकवरट्वसन,बुटवलपळ,रपदेहीलेपेशगरेकोदररेटकोआधारमाकुल रकम . ,दB,ooo अभरप …

### `70b0d415dcf3` p.1 — scan-backed queue

- unresolved; act 2019
- blob: `70b0d415dcf306bc6640027aa64dbfd236418611b1806477fcdd232430af4fc3.pdf`
- rendered source: `docs/nrb/holdout-pages/70b0d415dcf3-p001.jpg`

**Existing text layer (pypdf)** — 0 Devanagari chars:

> \\.?tr{trT q q€ w ruq aFrerTq?T hflT ff,*.r <i+-{q srrcFarr q-Er;tfr +r{tqRr (zF) h*c a-frffi qfri r+TT Eeil qqR ET-tsR q-qrq-{ sfuki hqi<.T rt q-q1kq {ils r (q) fr&q q-6-q-i qki r+q 3n€rq-+] q-{fl.iqrq irE ae-+] ffi qTq( k€i-s r (rr) qi.qr{ sRTqel aqal qft etzifufh HTqT {6rq rrq q-q}kq qi.u r (q) fr*q q-frq-q-+l e}-d+,-d-f, qqeme< errfiqT rtts t (s) fr*c €-fi-d{ frqcF-+R{rr +rfr-{ frihqT q{eTr qq q*k{fir ("+.", "e"  …

**Existing legacy conversion** (from the review pack):

| in | out | disposition |
| --- | --- | --- |
| `w ruq aFrerTq?T hflT` | ध चगत्र बँचभचत्त्ररुत् जाति् | rejected |
| `ff,*.r <i+-{q srrcFarr q-Er;tfr +r{tqRr` | ाा,८।च ?ष्र्(ंत्र कचचअँबचच त्र(भ्चसताच ंर्चतत्रच्च | rejected |
| `(zF) h*c a-frffi qfri r+TT Eeil qqR ET-tsR q-qrq-{ sfuki hqi<.T rt q-q` | ९शँ० ज८अ ब(ाचााष् त्राचष् चंत्त् भ्भष्ि त्रत्रच् भ्त्(तकच् त्र(त्रचत्र | rejected |
| `(q) fr&q q-6-q-i qki r+q 3n€rq-+] q-{fl.iqrq irE ae-+] ffi qTq( k€i-s ` | ९त्र० ाच७त्र त्र(ट(त्र(ष् त्रपष् चंत्र घल€चत्र(ें त्रर्(ा।िष्त्रचत्र ष | rejected |

**Docling OCR** — 1650 Devanagari chars:

> l pih राष्ऋणथवरथापनिवभाग नभेपसंकलनउपकरणसमबधीकायिविध - नभेपसंकलनगिरनेरकमखुलाबजारकारोबारसंचालनसिमितलेिनधिरणगरेबमोिजमहुनेछ क - ख िनभेपसंकलनगिरनेरकमआहानकोसूचनानेपालराष्बैंककोवेवसाईटमाफतिदईनेछ - ग I sy? Hiye Nh: pph hil rjufle Eh ii ehIh bhble - घ िनभेपसंकलनकोबोलकबोलबहुलाजदरपणालीमागिरनेछ - िनभेपसंकलनबोलकबोलमामौदकनीतमायवरथाभएबमोिजमकाकाउपटरपाटीहरक,खरग बगकाबैंकतथािवतीयसंरथाहलेमाघसहभागीहुनपाउनेछन् - च काउणटरपाटीहरलेबोलफाराममा …

### `2e65dadfffa3` p.1 — scan-backed queue

- partial, Devanagari-after 0.1996; 150 dpi
- blob: `2e65dadfffa3e740eb0292c2c96524ba6ae5642d016f20d557dfa8228f100969.pdf`
- rendered source: `docs/nrb/holdout-pages/2e65dadfffa3-p001.jpg`

**Existing text layer (pypdf)** — 0 Devanagari chars:

> ffiw ilrM trg +fi Htqft Hfrqq aFrerr{ hrn qrqnr+ Tqlrgr {nqT ffiq q;rqi"-q ?]Sffi, 6T6qr€1 qt{: Yjc,coY /\/\s Ylolo1 /Yl01{c/Yll1to gffie=${: lqi \Tl-4S: ool\ee 1 YYIY{{i frE f,ffi: \ei Email :km@nrb.org.np fi+fu:RorglzrgzqTTd'qr:k.3IT.9.1R / Yq / oel / \eY q+ Erfrrq +firt kqq : qfilfrT{fir+1 Eil k{rq q6rsi sqnrqr r rr6rqr{T qq 3{fu rrr.il+1 +tq+rorzakqr q-<rrn6 & ffi {qpnirA B{Frfl kqid o,rimr T{€+tqr arq kfirqrkffi …

**Existing legacy conversion** (from the review pack):

| in | out | disposition |
| --- | --- | --- |
| `Htqft Hfrqq aFrerr{ hrn` | ज्तत्रात जचत्रत्र बँचभचर्च जचल | ambiguous |
| `\Tl-4S: ool\ee 1 YYIY{{i` | ्त्(िद्धक्स् ययि्भभ ज्ञ थ्थ्क्ष्थर्र््ष् | rejected |
| `fi+fu:RorglzrgzqTTd'qr:k.3IT.9.1R / Yq / oel / \eY` | ाष्ांगस्च्यचनशिचनशत्रत्त्मुत्रचस्प।घक्ष्त्।ढ।ज्ञच् र थ्त्र र यभि र ्भथ | rejected |
| `kqq : qfilfrT{fir+1 Eil k{rq q6rsi sqnrqr r` | पत्रत्र स् त्राष्िाचतर््ष्चांज्ञ भ्ष्ि र्पचत्र त्रटचकष् कत्रलचत्रच च | rejected |

**Docling OCR** — 667 Devanagari chars:

> Boll pi वदेशीिविनमययवरथापनवभाग आनतिरकपशासनशाखा पaसंखय:fd.आ.प. Or? o0rIG? YPbe holnjie gl िवषयःपिततपवहककोछुहिववरणपठाउनेसमबधमा Hlip! यसअिघभारतकोकोलकातााहिदयाबदरगाहहुँदैतेसोमुलुकसँगकोआयातिनयितकारोवार भइरहेकोमाहालिवसाखापतनमबदरगाहबाटआयातिनयितकारोवारगनीनेपालभारतदुईदेशिबच सहमितभएकोसदभीमािवसाखापतनमबदरगाहहुदैमालबरतुआयातगनीसिकनेउचबदरगाहहुद आयातहुनेमालवरतुनेपालकािवराटनगर,वीरगनज,भैरहवारनेपालगजभसारनाकाहुदैनेपाल भव्याउनसिकनेरवािणज …

### `360eaafd44bd` p.1 — scan-backed queue

- partial; highest English share in band (8.9%)
- blob: `360eaafd44bdf4addde8bfc2b4582e687a5e1f846498e8541811cda7c4504707.pdf`
- rendered source: `docs/nrb/holdout-pages/360eaafd44bd-p001.jpg`

**Existing text layer (pypdf)** — 0 Devanagari chars:

> irrn Aq dt{T {rq frfr, {err Frqqq qfl qrqr : t.h.fr-.fu. zffizqRqzTa$r/11/ o\st(/ \e\e {qIf,f,qarr.r.w aq;rr Trffit 44-6{, ,+Ctq arqieq qryarcR, araqrg'i qTc YYll Yo'e qAT4TI; YYiY{{! E-mail <nrbbfirdppd@nrb org np> Web Site : www.nrb org nP q'ir4 A4q \9j ffi; loeq /11/ ?1 ffiqiI|.ffi-q {wtt fr+T-{ trd \. *F 'iiqil-Ql{t, ffi qr{Tfcrdrr ra-+1 qq a'mqT f{M wql qqT tr{ S"fr hq-{qr frtflTf, qz{-<rtt rrt q<{qr qq t-{-{rd  …

**Existing legacy conversion** (from the review pack):

| in | out | disposition |
| --- | --- | --- |
| `qfl qrqr : t.h.fr-.fu. zffizqRqzTa$r/11/ o\st(/ \e\e` | त्राि त्रचत्रच स् त।ज।ाच(।ाग। शााष्शत्रच्त्रशत्ब४चरज्ञज्ञर य्कत९र ्भ्भ | rejected |
| `E-mail <nrbbfirdppd@nrb org np>` |  | kept_english |
| `Web Site : www.nrb org nP` |  | kept_english |
| `ffi qr{Tfcrdrr ra-+1 qq a'mqT f{M wql qqT tr{ S"fr hq-{qr frtflTf,` | ााष् त्रर्चतअचमचच चब(ंज्ञ त्रत्र mबुत्रत् र्ःा धत्रि त्रत्रत् तर्च क्ा | rejected |

**Docling OCR** — 1198 Devanagari chars:

> Yal pih! कामीम बैकतथावतीयसंरथािनयमनिवभाँग ऐषािधसीप पa संयाबै.व.न.व. नीत पिरपaIकखग qशoछ छए इजाजतपघपातकवगकाबैंकहर महाशयु नेपालीनागिरकसँगरहेकोसुनबैंकमािनभेपकोपमाजमागनीसवनेवषयमानीतगत यवरथागनेसदभीमायसबैंकवाटकखरगवगकाइजाजतपनपातसंरथाहलाईजारी गिरएकोएकीकृतिनदेशनर॰ए कोइ.पा.नदेशनशा०प मादेहायबमोिजमबुँदान.न थप Je aRot B ro al pihy ? Pailr pir Hijhe leu Ip? ehy Piph larh gr दफाणरलेिदएकोओिधकारपयोगगरीयोिनदेशनजारीगिरएकोछ डःनेपालीनागिर …

### `a17fa322b81a` p.1 — scan-backed queue

- partial; circular 2021
- blob: `a17fa322b81a2ff7a7b7181d4564a742510c69536965d71a768d1396f09b9d02.pdf`
- rendered source: `docs/nrb/holdout-pages/a17fa322b81a-p001.jpg`

**Existing text layer (pypdf)** — 0 Devanagari chars:

> iqrn rq +n ffiqq1qf"o f{tqft kfurq q{eTrr{ kqr.r E.fi. qfurl Sqr : ly/Roee-eq fuk: loec /ol/1o * A-rtq rrE *+, +f+-f, hlrrr Frqr.ki q+qoi qtqTftrrfr zr'r-zltdq-6r, EwvriilrNrlrqi tq;" !r-Tiz[-[ ilfurq +drf{ r nEq<rc+r (G[' ili+-[ k+.rq il+-m r ftqq ; \rftf"il qftq{-1o\s(r qr qqfurq rrftg+1 q-Er+rrtt I q6[qTq, kqft kfr{q zrrt-qR rt=i rcffiflcrct-w t+ a+r fffiq inqr r rq ffi qrtt rrtrq+1 q-+t6-d qfrqr-1oeq qr iiar+Efrk …

**Existing legacy conversion** (from the review pack):

| in | out | disposition |
| --- | --- | --- |
| `f{tqft kfurq q{eTrr{ kqr.r` | र्तात्रात पागचत्र र्त्रभत्चर्च पत्रच।च | ambiguous |
| `E.fi. qfurl Sqr : ly/Roee-eq fuk: loec /ol/1o` | भ्।ाष्। त्रागचि क्त्रच स् थिरच्यभभ(भत्र ागपस् यिभअ रयरिज्ञय | rejected |
| `* A-rtq rrE *+, +f+-f, hlrrr Frqr.ki q+qoi qtqTftrrfr zr'r-zltdq-6r, E` | ८ ब्(चतत्र चचभ् ८ं, ां(ा, जचिचच ँचत्रच।पष् त्रंत्रयष् त्रतत्रततचचाच शच | rejected |
| `ilfurq +drf{ r nEq<rc+r (G[' ili+-[ k+.rq il+-m r` | ष्िागचत्र ंमर्चा च लभ्त्र?चअंच ९न्ुृ ष्ष्िं(ृ पं।चत्र mष्िं( च | rejected |

**Docling OCR** — 1499 Devanagari chars:

> महाशयु वदेशीिविनमयकारोबारगनीइजाजतपघपातबैंकतथािवतीयसंरथारअयिनकायलाईजारीगिरएको एकीकृतपिरपऋ-॰णमादेहायबमोिजमसंशोधनगिरएकोहुँदासोहीवमोिजमगनुगराउनुहुनिवदेशीिविनमय वनयिमतगनेऐन,॰परकोदफापशलेिदएकोअिधकारपयोगगरीयोपिरपऋजारीगिरएकोछा एकीकृत पिरप-२o को एकीकृत s.पा. पिरप: OाRo७ को दफा O.9 को उप-दफा A.9. लाई देहायबमोिजमसंशोधनगिरएकोछ यरतोसटहीसिवधाबढीमाएकपटकमाअमेिरकीडलरपoठठपैतीसहजार वासोबराबरकोपिरवयी वदेशीमुदासमकोलािगमाऋपदानगनीसिकनेछपयसर …

### `8e8467f74f84` p.1 — scan-backed queue

- partial; forex 2018
- blob: `8e8467f74f84713299c43d05a18261ffd23c2a6de35af71413c4699823518e66.pdf`
- rendered source: `docs/nrb/holdout-pages/8e8467f74f84-p001.jpg`

**Existing text layer (pypdf)** — 0 Devanagari chars:

> iqm TE t6ffi{ srqt--rq fqtfi f$rrq aFrsffi{ tmr{r Ernrq ffi qftFrd Ht{fi Tfrrqr EEUI kf, vfr ET{st sqmqffir ktqt Ht+rq tFrqka qil tt, toq<, +1 EsT qocr. A @r BIftmR !r*{r rrft qs *+-Ere Eqt,ldr{ sTqr rrt6r qrftrq ffi tarrfir qrffi qr+cr *rf, rrft hht *+ aqr ffiq senrtqre qfrsd Rft gnwr aq kr rfr q+en rrMA {qftra s*+1 qr++rffi qrfu m qrffi+, qfin rsrqr{ rM a r {rd6{ (1) qs *+qre sflukrr{ 9rq r*' T{t+l ffi nffiq vqt ts …

**Existing legacy conversion** (from the review pack):

| in | out | disposition |
| --- | --- | --- |
| `iqm TE t6ffi{ srqt--rq` | ष्क्र त्भ् तटााषर्् कचत्रत((चत्र | rejected |
| `fqtfi f$rrq aFrsffi{ tmr{r` | ात्रताष् ा४चचत्र बँचकााषर्् mतर्चच | rejected |
| `Ernrq ffi qftFrd Ht{fi Tfrrqr EEUI kf, vfr ET{st` | भ्चलचत्र ााष् त्रातँचम र्ज्ताष् तचचत्रच भ्भ्ग्क्ष् पा, खाच भ्तर््कत | rejected |
| `ktqt Ht+rq tFrqka qil tt, toq<, +1 EsT qocr. A @r BIftmR !r*{r rrft qs` | पतत्रत ज्तंचत्र तँचत्रपब त्रष्ि तत, तयत्र?, ंज्ञ भ्कत् त्रयअच। ब् २च m | ambiguous |

**Docling OCR** — 1429 Devanagari chars:

> e Al piht Wieler hlye? Iee! Ehlhaphe hh! U6Pe! Thaphe ph Br! lore HIsh 1e?e! geh pepee holofie सरबधीसूचना वदेशीिविनमयतनयिमतगनेऐन,शणशकोदफापoख.लेिदएकोओंधकारपयोगगरीयसबैंकबाटइजाजतपनपात गरेकाबािणयबैकहरलेदेहायकाशतहलकोपालनागनेगरीिवदेशीबैंकतथािवतीयसंरथाहरबाटपिरवलीवदेशीमुदामा ऋणिलनसवनेशवरथागिरएकोलेसमबिधतसबैकोजानकारीकोलािंगयोसावेजिनकसूचनापकाशनगिरएकोछ शतंहर - यसबैंकबाटइजाजतपघपातकवगकाबैकहरलेनवीकरणीयउजि्जलिवधुत,सौयीरवायुउजिउलपादन …

### `438c55304da5` p.1 — needs_ocr

- no_text_layer; exam result 2025
- blob: `438c55304da52580dd6314b01e8db71d3a3a9023abd9265cd99fd9ada3f21152.pdf`
- rendered source: `docs/nrb/ocr-spike-pages/438c55304da5-p001.jpg`

**Existing text layer (pypdf)** — 0 Devanagari chars:

> *(empty)*

**Docling OCR** — 784 Devanagari chars:

> p 201 pine Iep! Ehorpre EaIl piih. सूचना बैंकमासहयोगीसेवातफिरकरहेकोइिजिनयरपदकरारमाफतपितंगनेसबधमािमितश॰टशश॰छमा पकािशतसूचनाबमोिजमरीतपुवेकदरखारतिदईबैंकबाटसउचालनगिरएकोपरीभामासहभागीभएका उमेदवारहलेपामगरेकोअङ्ककाआधारमासफलभएकातपिशलकामुखयउमेदवारलाईकरारमािनयिक गनेिनणियभएकोहुँदासमबिधतसबैकोजानकारीकोलिगयोसूचनापकािशतगिरएकोछा वज़ापननं.१॰/२॰८, Ahuiu ph C roph IIh अ | रोलनं. उममेदवारकोनामथर - ५१ पिदपभहराई | |-------------------------- …

### `c298efaf1f16` p.1 — needs_ocr

- no_text_layer; notice 2078.12.10
- blob: `c298efaf1f16a8e8f10424636769a8a7effe1eba81bbfcd0a1328a6924764156.pdf`
- rendered source: `docs/nrb/ocr-spike-pages/c298efaf1f16-p001.jpg`

**Existing text layer (pypdf)** — 0 Devanagari chars:

> *(empty)*

**Docling OCR** — 1234 Devanagari chars:

> अनुसूचीन लगानीसःबधीसूचना यसबैककोविभलकोषहलमारहेकोरकमलगानीगनुपनेभएकोलेयसबैकबाटइजाजतपघपातदेहाय बमोिजमकाशतहरपुरागरेकाबैकतथािवतीयसंरथाबाटदेहायबमोिजमकोसीमिभघरहीआफूलेिलनचाहेको Hah thiyhe uiia e! hl Wh Dh Ibolhe lhjl bi leiz lb heh! pa hl hh? भरी उच फारमलाई df format मा Scan गरी Password Protect गरर कुल लगानीयोरय रकम मeय fमित २o५ वैशाख श गत दख रooवैशाख a तदनुसार ञपल aर, oरशदिख ओिपलaर, Roशश सम ३x दनक Eijee Oo:b pi pr obabsoot …

### `276b2eb62802` p.1 — needs_ocr

- no_text_layer; calendar 2020
- blob: `276b2eb6280234402e8495cd1f8dc4b436c11f032acccac00ad603027694b623.pdf`
- rendered source: `docs/nrb/ocr-spike-pages/276b2eb62802-p001.jpg`

**Existing text layer (pypdf)** — 0 Devanagari chars:

> *(empty)*

**Docling OCR** — 364 Devanagari chars:

> We pl siht सामादयासेवािवभाग दरभाउपञखीकृतभएकोसूचना L albaLfis IeE th aih h++ eih Be! tbin ooot i E lyis Ee+e th. आपितगनसछधमामितर्कपनालशगतगोरखापनापकिशतसलवदीदरमाउपन आह्वानकोसूचनानमिजमपशनमपकातदरभाउपधहरमधयिननसामानरसकालाा Piolt bihm yheipipehelbr Ehpp Iineiek Ith? pil pgihsh Whe भपकोलनेपालशाट्वैकखिरदनिविनयमावलीनशनसंशोधनसमतमकोिविनयमरण कोचपिनिनयमवशाकोमयोजनकोलिंगयोसूचनान्पकिशतागिरारकोछ | Thihit. Wileeheiet कस haE! धान r Ihil  …

### `1a9b6321aa61` p.1 — font-embedded control

- recovered; Preeti+Bishall; 10/10 units shown
- blob: `1a9b6321aa6124efdd618348ef1184f9dd5e57549c5ff0f7479b09226e986866.pdf`
- rendered source: `docs/nrb/holdout-pages/1a9b6321aa61-p001.jpg`

**Existing text layer (pypdf)** — 0 Devanagari chars:

> sf/jfxL km's'jf ePsf ljQ sDkgLx?sf] ljj/)f M != c?0f kmfOgfG; lnld6]8 w/fg, ;'g;/LnfO{ ul/Psf] sf/jfxL km's'jf . c?0f kmfOgfG; lnld6]8nfO{ g]kfn /fi6 a}+s, a}+s tyf ljQLo ;+:yfsf] zL3| ;'wf/fTds sf/jfxL ;DaGwL ljlgodfjnL, @)^$ sf] ljlgod # sf] -v_ adf]lhd ldlt @)^(.!.#! sf] lg0f{ofg';f/ ul/Psf] zL3| ;'wf/fTds sf/jfxL ldlt @)^(.@.@$ b]lv km's'jf ul/Psf] 5 . @= ;[hgf kmfOgfG; lnld6]8 lj/f6gu/nfO{ ul/Psf] sf/jfxL km's'j …

**Existing legacy conversion** (from the review pack):

| in | out | disposition |
| --- | --- | --- |
| `sf/jfxL km's'jf ePsf ljQ sDkgLx?sf] ljj/)f M ` | कारवाही फुकुवा भएका वित्त कम्पनीहरुको विवर०ा :  | rejected |
| `!= c?0f kmfOgfG; lnld6]8 w/fg, ;'g;/LnfO{ ul/Psf] sf/jfxL km's'jf . ` | १. अरुण फाइनान्स लिमिटेड धरान, सुनसरीलाई गरिएको कारवाही फुकुवा ।  | ambiguous |
| `c?0f kmfOgfG; lnld6]8nfO{ g]kfn /fi6 a}+s, a}+s tyf ljQLo ;+:yfsf] zL3` | अरुण फाइनान्स लिमिटेडलाई नेपाल राष्ट बैंक, बैंक तथा वित्तीय संस्थाको श | converted |
| `;DaGwL ljlgodfjnL, @)^$ sf] ljlgod # sf] -v_ adf]lhd ldlt @)^(.!.#! sf` | सम्बन्धी विनियमावली, २०६४ को विनियम ३ को (ख) बमोजिम मिति २०६९।१।३१ को  | converted |

**Docling OCR** — 540 Devanagari chars:

> कारवाहीफुकुवाभएकािवतकमपनीहरकोिववरण प.अकणफाइनासिलिमटेडधरान,सुनसरीलाईगिरएकोकारवाहीफुकुवा अरणफाइनासिलिमटेडलाईनेपालराषटबैंक,बैंकतथािवतीयसंरथाकोशीघसुधारामककारवाही समबधीिविनयमावली,रदद कोिविनयमशको ख बमोिजमिमितर०दशाशप कोिनणयानुसारगिरएको शीघसुधारामककारवाहीिमित२॰शाश२दिखफुकुवागिरएकोछ . सृजनाफाइनासिलिमटेडिवराटनगरलाईगिरएकोकारवाहीफुकुवा सृजनाफाइनासिलिमटेडलाईसमानुपाितकचुवतापूँजीवृिदनगरेकोहुनालेयसबैंककोएकीकृत नदेशन,२०७ कोिनदेशननःश क …

### `d1c99f3cf34d` p.1 — font-embedded control

- recovered; smallest in queue, 9/9 units
- blob: `d1c99f3cf34decda9d0f695b549765b6669e28eaf2eedc4dac952ded2acc0446.pdf`
- rendered source: `docs/nrb/holdout-pages/d1c99f3cf34d-p001.jpg`

**Existing text layer (pypdf)** — 0 Devanagari chars:

> g]kfn /fi6« a}+s df]x/ l8lh6n k|f=ln= sf] cg'dltkq vf/]h ul/Psf] ;"rgf . o; a}+saf6 e'QmfgL ;]jf k|bfossf] ?kdf sfo{ ug]{ u/L ldlt @)&^÷)^÷#) df cg'dltkqk|fKt df]x/ l8lh6n k|f=ln= n] e'QmfgL tyf km:of}{6 P]g, @)&% sf] bkmf #^ sf] v08 -5_ / -h_ adf]lhdsf] s;'/ u/]sf] b]lvPsf]n] e'QmfgL tyf km:of}{6 ljlgodfjnL -k|yd ;+zf]wg, @)*)_, @)&& sf] ljlgod #% sf] v08 -u_ sf] Joj:yf adf]lhd pQm ;+:yfsf] cg'dltkq vf/]h ul/Psf] Jo …

**Existing legacy conversion** (from the review pack):

| in | out | disposition |
| --- | --- | --- |
| `df]x/ l8lh6n k\|f=ln= sf] cg'dltkq vf/]h ul/Psf] ;"rgf . ` | मोहर डिजिटल प्रा.लि. को अनुमतिपत्र खारेज गरिएको सूचना ।  | converted |
| `o; a}+saf6 e'QmfgL ;]jf k\|bfossf] ?kdf sfo{ ug]{ u/L ldlt @)&^÷)^÷#) d` | यस बैंकबाट भुक्तानी सेवा प्रदायकको रुपमा कार्य गर्ने गरी मिति २०७६/०६/ | converted |
| `df]x/ l8lh6n k\|f=ln=  n] e'QmfgL tyf km:of}{6 P]g, @)&% sf] bkmf #^ sf` | मोहर डिजिटल प्रा.लि.  ले भुक्तानी तथा फर्स्यौट ऐन, २०७५ को दफा ३६ को ख | converted |
| `adf]lhdsf] s;'/ u/]sf] b]lvPsf]n] e'QmfgL tyf km:of}{6 ljlgodfjnL  -k\|` | बमोजिमको कसुर गरेको देखिएकोले भुक्तानी तथा फर्स्यौट विनियमावली  (प्रथम | converted |

**Docling OCR** — 385 Devanagari chars:

> मोहरिडिजटलपा.िल.कोअनुमितपऋखारेजगिरएकोसूचना यसबैंकबाटभुकानीसेवापदायककोरपमाकायीगनेगरीिमितoछशणशाः9्माअनुमितपघपात मोहरिडिजटलपा.ल.लेभुकानीतथाफरयौटऐन,oपकोदफा कोखuड छ रज बमोिजमकोकसुरगरेकोदिखएकोलेभुकानीतथाफरयौटविनयमावलीपथमसंशोधन,॰दण, hfi Eaip thPihpie lk pa Huh hrbre O pnb e Hhe oooc यहोरासमबिधतसबैकोजानकारीकालािंगयोसूचनापकाशनगिरएकोछाउकसंरथालेजारी गरेकाभुकानीउपकरणहरकोपयोगगनेयाहकहरसंगकोकुनैदियलबाँकीरहेसमबिधतसंरथामा समपकीगनुहुन …

### `268bcfe86d03` p.1 — font-embedded control

- partial; circular 2007
- blob: `268bcfe86d03e9ef684b47f17345a5c0b8e072cdc92f71efca45d6c9e29937b2.pdf`
- rendered source: `docs/nrb/holdout-pages/268bcfe86d03-p001.jpg`

**Existing text layer (pypdf)** — 0 Devanagari chars:

> g]kfn /fi6« a}+s s]Gb|Lo sfof{no ljb]zL ljlgdo Joj:yfkg ljefu O=k|f=kl/kq ;+VofM– #(& ldlt M– @)^#÷!@÷!! Ohfhtkqk|fKt æsÆ / /fli6«o:t/sf ævÆ ju{sf a}+s tyf ljQLo ;+:yfx¿ . ljifo M– :yfgLo ljqm]tfn] ko{6snfO{ ljqmL u/]sf] ;fdfgsf] lgof{tsf] clu |d e'QmfgL k|df0fkq hf/L ug]{ af/] . dxfzo, ko{6sn] :yfgLo ljqm ]tfaf6 vl/b u/ ]sf] ;fdfg lgof {t k|lqmof ckgfO{ :yfgLo ljqm ]tf kmd{n] g } ko {6ssf] 7 ]ufgfdf k7fpg ] ePdf To: …

**Existing legacy conversion** (from the review pack):

| in | out | disposition |
| --- | --- | --- |
| `ljb]zL ljlgdo Joj:yfkg ljefu ` | विदेशी विनिमय व्यवस्थापन विभाग  | ambiguous |
| `O=k\|f=kl/kq ;+VofM– #(& ldlt M– @)^#÷!@÷!! ` | इ.प्रा.परिपत्र संख्याः– ३९७ मिति :– २०६३/१२/११  | ambiguous |
| `Ohfhtkqk\|fKt æsÆ / /fli6«o:t/sf ævÆ ju{sf a}+s tyf ljQLo ;+:yfx¿ . ` | इजाजतपत्रप्राप्त “क” र राष्ट्रियस्तरका “ख” वर्गका बैंक तथा वित्तीय संस | converted |
| `ljifo M–  :yfgLo ljqm]tfn] ko{6snfO{ ljqmL u/]sf] ;fdfgsf] lgof{tsf] ` | विषय :–  स्थानीय विक्रेताले पर्यटकलाई विक्री गरेको सामानको निर्यातको  | converted |

**Docling OCR** — 486 Devanagari chars:

> इ.पा.पिरप संछया.- ३१७ भितः-दश4 I yelo? hlpe! or ee lejfe Be, lePrrfl I , PllshPele? YPjr guih palr yp! piveyh pipyp hlio? -- Whe! अियमभुकानीपमाणपऋजारीगनेबारे महाशयु पयटकलेरथानीयिवकेताबाटखिरदगरेकोसामानिनयितपिकयाअपनाईरथानीयिवकेता फमलेनैपयटककोठेगानामापठाउनेभएमायरतोसामानहरकोभुकानीवापतपयटकले बैंकािवतीयसंरथामापिरवयीिवदेशीमुदाबुझाउनलयाएमािवदेशीमुदाखवीकारगरीसोकोहुने Pye khlolh yipi Hiyele ePiu! Bi h hiPi eph Pue! hi Ih? Frlo …

## 5. The six questions this spike was asked

1. **Can it read the problematic pages?** Yes. All 8 scan-backed queue pages went from **0** Devanagari characters to hundreds; the existing pipeline recovers nothing there because there is nothing to recover.
2. **Is it usable Unicode Nepali?** Script yes, orthography no — see §3. Usable as a coarse signal, not as text a reader or an index should trust.
3. **Which backend ran?** Docling OCR stage → RapidOCR → **`torch`** backend → **PP-OCRv4 `devanagari_..._rec_mobile`**. No new Python package: torch was already installed. PP-OCRv5 Devanagari was **not** reachable — docling maps the torch backend to PP-OCRv4 only, and v5 needs `onnxruntime`, which is absent.
4. **How slow?** 0.9s/page median after warmup (0.6–1.2s), on GPU. The first page pays 20s of model load and compile.
5. **Does it beat Preeti conversion on the failure cases?** On the scan-backed pages there is nothing to beat — conversion cannot apply. On the font-embedded controls it clearly **loses** to the existing converter (§3).
6. **Good enough to become the fallback?** Not as it stands. It earns a place only for pages with no legacy text layer. See the recommendation.

## 6. Recommendation

**Adopt nothing yet, and do not make this the general fallback.** What the evidence supports is narrower and worth having:

* **Give the future conversion router a font-provenance precondition.** `pdffonts`+`pdfimages` separate the populations (§1), cost milliseconds, need no model, and — importantly — need **no classifier change**: the check belongs to the router, downstream of native-2. That split is worth keeping whatever OCR wins.
* **Conversion stays the primary path** for the 45 font-embedding PDFs (44 with a recognised legacy family, 1 with stripped names) and is not in question for the 3 spreadsheets. This OCR is measurably worse on that population.
* **Before benchmarking PaddleOCR-VL, try the cheap upgrades on this same 14 pages**: the PP-OCRv4 *mobile* recogniser is the smallest in the family, and PP-OCRv5 Devanagari exists but needs the `onnxruntime` backend (a real pip dependency); docling's OCR render `scale` is also at its 3.0 default. If v5 at a higher scale still shows §3's conjunct collapse, the defect is the model class, not the configuration — and that is the point to benchmark a VLM OCR such as PaddleOCR-VL, which produces logically-ordered spaced text by construction.
* **Whatever wins still needs a Nepali reader** before any of it is indexed. §3 is a well-formedness argument, not a correctness one.

## 7. Evaluation & Improvement

**Success metric.** Share of scan-backed queue blobs for which OCR yields text a Nepali reader marks usable, where the current path yields none. Structural proxy until a reader returns: Devanagari codepoints recovered on pages whose existing text layer has zero.

**Eval.** The 14 named pages above — 8 scan-backed queue blobs, 3 `needs_ocr`, 3 font-embedded controls where the existing path already works. Scored structurally here; scored semantically only by a reader. The controls are the guard against adopting a fallback that is worse than what it falls back from.

**Feedback capture.** This file. Reader corrections and per-page disagreements belong here beside the excerpt they refer to, and feed the OCR-routing decision — never a native-2 retune.

**Review loop.** On any OCR backend or model change, and before OCR is wired into any pipeline. The provenance split in §1 was measured on the Phase 6B holdout, so it is **development evidence for the conversion router**, not independent validation of one. Since the split lives downstream of the classifier it forces no `native-3` and no fresh cohort; but the moment anything here is used to change native-2 itself, §14.7 and §15.5 apply in full and a new cohort is required.
