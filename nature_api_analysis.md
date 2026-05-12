# Nature Journal API Structure Analysis
**Date**: 2026-05-12T13:56:00.789908
**Paper**: 10.1038/s41586-026-10400-2

## 1. Redirect Chain

Step 1: `https://doi.org/10.1038/s41586-026-10400-2` → `https://www.nature.com/articles/s41586-026-10400-2` (302)
Step 2: `https://www.nature.com/articles/s41586-026-10400-2` → `https://idp.nature.com/authorize?response_type=cookie&client_id=grover&redirect_uri=https%3A%2F%2Fwww.nature.com%2Farticles%2Fs41586-026-10400-2` (303)
Step 3: `https://idp.nature.com/authorize?response_type=cookie&client_id=grover&redirect_uri=https%3A%2F%2Fwww.nature.com%2Farticles%2Fs41586-026-10400-2` → `https://idp.nature.com/transit?redirect_uri=https%3A%2F%2Fwww.nature.com%2Farticles%2Fs41586-026-10400-2&code=0c2932dd-15a4-4223-9126-2f6dd2ea7f7a` (302)
Step 4: `https://idp.nature.com/transit?redirect_uri=https%3A%2F%2Fwww.nature.com%2Farticles%2Fs41586-026-10400-2&code=0c2932dd-15a4-4223-9126-2f6dd2ea7f7a` → `https://www.nature.com/articles/s41586-026-10400-2` (302)
Step 5: `https://doi.org/10.1038/s41586-026-10400-2` → `https://www.nature.com/articles/s41586-026-10400-2` ((automatic))

**Final URL**: https://www.nature.com/articles/s41586-026-10400-2

## 2. Network Requests Summary

**Total Requests**: 56
**Total Responses**: 50

### Request Types

- **script**: 21 requests
- **image**: 10 requests
- **fetch**: 8 requests
- **document**: 7 requests
- **font**: 4 requests
- **xhr**: 3 requests
- **stylesheet**: 2 requests
- **other**: 1 requests

### API Endpoints (11 requests)

- `GET` https://www.nature.com/platform/contextual?doi=10.1038/s41586-026-10400-2
- `GET` https://idp.nature.com/exposed-details
- `GET` https://ep1.adtrafficquality.google/getconfig/sodar?sv=200&tid=gpt&tv=m202605060101&st=env&sjk=8045887748270486
- `GET` https://pagead2.googlesyndication.com/gampad/ads?pvsid=8045887748270486&correlator=3432333572225030&output=ldjh&gdfp_req=1&vrg=202605060101&ptt=17&impl=fifs&ltd=1&npa=1&iu_parts=270604982%2Cnature%2Carticles&enc_prev_ius=%2F0%2F1%2F1%2F2&prev_iu_szs=728x90&ifi=1&didk=4224914489&dids=div-gpt-ad-top-1&adfs=59990112&sfv=1-0-45&eri=1&sc=1&abxe=1&dt=1778608558400&lmt=1778608558&adxs=8&adys=46&biw=1920&bih=1080&scr_x=0&scr_y=0&btvi=0&ucis=1&oid=2&u_his=2&u_h=1080&u_w=1920&u_ah=1080&u_aw=1920&u_cd=24&u_sd=1&u_tz=-240&dmc=8&bc=31&nvt=1&uach=WyJMaW51eCIsIiIsIng4NiIsIiIsIjE0NS4wLjc2MzIuNiIsbnVsbCwwLG51bGwsIjY0IixbWyJOb3Q6QS1CcmFuZCIsIjk5LjAuMC4wIl0sWyJIZWFkbGVzc0Nocm9tZSIsIjE0NS4wLjc2MzIuNiJdLFsiQ2hyb21pdW0iLCIxNDUuMC43NjMyLjYiXV0sMF0.&url=https%3A%2F%2Fwww.nature.com%2Farticles%2Fs41586-026-10400-2&vis=1&psz=1904x119&msz=1904x0&fws=4&ohw=1920&dlt=1778608557023&idt=1237&prev_scp=type%3Darticle%26pos%3DLB1%26articleid%3Ds41586-026-10400-2%26doi%3D10.1038%252Fs41586-026-10400-2%26techmeta%3D119%2C123%2C128%2C129%2C132%2C140%2C144%26subjmeta%3D1135%2C1960%2C3923%2C400%2C639%2C766%26kwrd%3DHigh-harmonic%252Bgeneration%2CLaser-produced%252Bplasmas%26bpid%3D2000481557%2C8200828607%2C3005732355%26campaignID%3D20250910_3593%2C20250922_1656%2C20251212_0379%2C20260120_2314%2C20260321_3310%2C20260327_5522%2C20260327_5539%2C20260411_2060%2C20260415_7161%2C20260415_8041%2C20260429_9988%26logged%3Dn%26consent_analytics%3Dfalse%26consent_marketing%3Dfalse%26consent_ads%3Dfalse&adks=2050973735&frm=20
- `GET` https://pagead2.googlesyndication.com/pcs/view?xai=AKAOjsuTx8JmVarDQTyoDzt6RFEuF4qC3xIG_Ki7MHcx8Rss2GvA1M4_G7rcRA11f8DZhGeKwhfc_JRIuVVK2siVU22EQV33K44OMd456sAg3Cu3J7ee53PJ3pUpVAFkTb_Jtk5rChlcrhzV_GkizobWQDL2fPfqDGzkeIGGNBqaI8kcCQAN3VsaJKI4m27FkiaLmc_18RCHlJFeTezlqpoUNcty9ObIp59AaXW8FSzSGvNO1eL2K8jTPLs_lxWZKdxuMJRGtuS0gfZBjzWWktuYXvGfaCAAqHgvi9EmPaUhrnwVc2Qw6U83OnKbjGHEdx49UMFQ70hDAjOiLYE3s0AJk8enk30swvS5uzLq6sRhJISr2d_MagYGqgnfcOmuk8-JAlw14FkFh_qov44tEQK_l9ZxNHK_a8iIKNrF1aPIAUIqCM1WxoWC4IJdufgn1H1sU48UFEQ&sig=Cg0ArKJSzOcwOqUFlFZMEAE&uach_m=%5BUACH%5D&adurl=
- `GET` https://pagead2.googlesyndication.com/pagead/gen_204?id=av-js&type=fle-fetch-start2
- `GET` https://pagead2.googlesyndication.com/pagead/gen_204?id=av-js&type=colleague-executed&name=4
- `GET` https://pagead2.googlesyndication.com/pagead/gen_204?id=av-js&type=reach&proto=CAlgAWACaAM%3D
- `GET` https://pagead2.googlesyndication.com/pcs/view?xai=AKAOjss3KkPewe4IWCed9_C0Mbufb4GRIcT65Qr-MCqO7ZxdyGP9Ct6_-mi_H1K0lHucv_jw1rWXFXo7MUVBtL_qm58KtrFJMRcDLLf0V57ouxJKs_8grY12M5owUXTy_uVyuGv2kuDUUfTgq6-wKsNuIOFBlfmKtMNWDRq_mOw1hXtXtz2GenaSn4cj0w0Pd2JJnB19IwE1IH0bBqBudx97-MoJVJKSuZvjDF376jGrQVW_yT9lIyLi54uAZBciBVhR4nGiZjoMLkLHR-zShLfjsnNybiE2FhlkERCVgpXYBAyaSSXXk-bvsg5BZx8pxmuEV3bole2RcIlJzeHGqtPbuTiQ5IKQ2UOL1KwWhD5rEguvp5LtREg66tUdswgADVfAp5zFtniCNkeLZYTcDF-NlvX_K1wY5DTrjrCKekDRLphfcylKwB38_w3jjK1G35tfToedYnIfQA&sig=Cg0ArKJSzFT9cXmpKto9EAE&uach_m=%5BUACH%5D&dett=2&adurl=
- `GET` https://pagead2.googlesyndication.com/pcs/activeview?xai=AKAOjst7BVH45C2dkEp39KJU11w0EXXbsSLti3m-lljgizpJosyqzsbABXmt5QX1C6BN9eojcyq7fxQ0txC1ut4OeH4hfp7oyN2HGV3yWGcsWgK6PpoTd7iO4wRDcBG_VCnWqpJ9fEBozfmlfCH4dA-P7XqlZJf5P5i4bY1rgY5KdPQOEDAEHi1CLqW2_TmLisM&sig=Cg0ArKJSzA2_XxYu05TyEAE&id=lidar2&mcvt=1001&p=45,596,135,1324&tm=1099.7000000476837&tu=99.20000004768372&mtos=1001,1001,1001,1001,1001&tos=1001,0,0,0,0&v=20260511&bin=7&avms=nio&bs=1920,1080&mc=1&vu=1&app=0&itpl=0&adk=2050973735&rs=4&la=0&cr=0&uach=WyJMaW51eCIsIiIsIng4NiIsIiIsIjE0NS4wLjc2MzIuNiIsbnVsbCwwLG51bGwsIjY0IixbWyJOb3Q6QS1CcmFuZCIsIjk5LjAuMC4wIl0sWyJIZWFkbGVzc0Nocm9tZSIsIjE0NS4wLjc2MzIuNiJdLFsiQ2hyb21pdW0iLCIxNDUuMC43NjMyLjYiXV0sMF0%3D&vs=4&r=v&co=7454135800&rst=1778608558584&rpt=366&isd=0&lsd=0&met=mue&wmsd=0&pbe=0&fle=0&vae=0&spb=0&sfl=0&ffslot=0&reach=8&io2=0
- `GET` https://pagead2.googlesyndication.com/pagead/gen_204?id=av-js&type=fle-fetch-later2

## 3. Metadata Locations

## 4. Response Status Summary

- **200**: 44 responses
- **204**: 2 responses
- **302**: 3 responses
- **303**: 1 responses

## 5. Detailed Requests Log

```
GET    document     https://doi.org/10.1038/s41586-026-10400-2
GET    document     https://www.nature.com/articles/s41586-026-10400-2
GET    document     https://idp.nature.com/authorize?response_type=cookie&client_id=grover&redirect_uri=https%3A%2F%2Fwww.nature.com%2Farticles%2Fs41586-026-10400-2
GET    document     https://idp.nature.com/transit?redirect_uri=https%3A%2F%2Fwww.nature.com%2Farticles%2Fs41586-026-10400-2&code=0c2932dd-15a4-4223-9126-2f6dd2ea7f7a
GET    document     https://www.nature.com/articles/s41586-026-10400-2
GET    image        https://media.springernature.com/full/nature-cms/uploads/product/nature/header-86f1267ea01eccd46b530284be10585e.svg
GET    font         https://www.nature.com/static/fonts/HardingText-Regular-Web-cecd90984f.woff2
GET    stylesheet   https://www.nature.com/static/css/enhanced-article-nature-branded-05ab29a23a.css
GET    stylesheet   https://www.nature.com/static/css/article-print-bfd4be62ef.css
GET    script       https://cmp.nature.com/production_live/en/consent-bundle-8-102.js
GET    script       https://www.nature.com/static/js/global-article-es6-bundle-78fa273879.js
GET    script       https://www.nature.com/static/js/shared-es6-bundle-6dcdb1d603.js
GET    script       https://www.nature.com/static/js/header-150-es6-bundle-0dfafc7bf6.js
GET    image        https://media.springernature.com/w215h120/springer-static/image/art%3A10.1038%2Fs41567-021-01253-9/MediaObjects/41567_2021_1253_Fig1_HTML.png
GET    image        https://media.springernature.com/w215h120/springer-static/image/art%3A10.1038%2Fs41598-022-11313-6/MediaObjects/41598_2022_11313_Fig1_HTML.png
GET    image        https://media.springernature.com/w215h120/springer-static/image/art%3A10.1038%2Fs41598-022-17762-3/MediaObjects/41598_2022_17762_Fig1_HTML.png
GET    image        https://media.springernature.com/lw685/springer-static/image/art%3A10.1038%2Fs41586-026-10400-2/MediaObjects/41586_2026_10400_Fig1_HTML.png?as=webp
GET    image        https://www.nature.com/static/images/logos/nature-briefing-logo-n150-white-afc2e6ccc7.svg
GET    script       https://content.readcube.com/ping?doi=10.1038/s41586-026-10400-2&format=js&last_modified=2026-04-22
GET    script       https://www.nature.com/static/js/math-es6-bundle-46c5b808b1.js
GET    font         https://www.nature.com/static/fonts/HardingText-Bold-Web.woff2
GET    font         https://www.nature.com/static/fonts/HardingText-Regular-Web.woff2
GET    font         https://www.nature.com/static/fonts/HardingText-RegularItalic-Web.woff2
GET    script       https://sgtm.nature.com/gtm.js?id=GTM-MRVXSHQ
GET    script       https://injections.readcube.com/config.json
GET    script       https://cdn.jsdelivr.net/npm/mathjax@2.7.5/MathJax.js?config=TeX-AMS-MML_SVG.js
GET    script       https://injections.readcube.com/nature/inject.fa744f3f.js
GET    script       https://cdn.jsdelivr.net/npm/mathjax@2.7.5/config/TeX-AMS-MML_SVG.js?V=2.7.5
GET    script       https://cdn.jsdelivr.net/npm/mathjax@2.7.5/jax/output/HTML-CSS/config.js?V=2.7.5
GET    script       https://www.googletagmanager.com/gtm.js?id=GTM-MRVXSHQ&gtg_health=1
GET    xhr          https://www.nature.com/platform/contextual?doi=10.1038/s41586-026-10400-2
GET    image        https://sgtm.springernature.com/collect-cmp?en=banner_visible&ec=undefined&dl=https%253A%252F%252Fwww.nature.com%252Farticles%252Fs41586-026-10400-2&dr=&dt=Efficiency-optimized%20relativistic%20plasma%20harmonics%20for%20extreme%20fields%20%7C%20Nature&sr=1920x1080&gtmid=GTM-MRVXSHQ
GET    script       https://pagead2.googlesyndication.com/tag/js/gpt.js
GET    script       https://scripts.webcontentassessor.com/scripts/93dabb8d80079a87fec7bb6f67b807fce90e1688f8957ad7ad152bfd58ea01c2
GET    script       https://crossmark-cdn.crossref.org/widget/v2.0/widget.js
GET    xhr          https://idp.nature.com/exposed-details
GET    script       https://pagead2.googlesyndication.com/pagead/managed/js/gpt/m202605060101/pubads_impl.js
GET    xhr          https://ep1.adtrafficquality.google/getconfig/sodar?sv=200&tid=gpt&tv=m202605060101&st=env&sjk=8045887748270486
GET    fetch        https://pagead2.googlesyndication.com/gampad/ads?pvsid=8045887748270486&correlator=3432333572225030&output=ldjh&gdfp_req=1&vrg=202605060101&ptt=17&impl=fifs&ltd=1&npa=1&iu_parts=270604982%2Cnature%2Carticles&enc_prev_ius=%2F0%2F1%2F1%2F2&prev_iu_szs=728x90&ifi=1&didk=4224914489&dids=div-gpt-ad-top-1&adfs=59990112&sfv=1-0-45&eri=1&sc=1&abxe=1&dt=1778608558400&lmt=1778608558&adxs=8&adys=46&biw=1920&bih=1080&scr_x=0&scr_y=0&btvi=0&ucis=1&oid=2&u_his=2&u_h=1080&u_w=1920&u_ah=1080&u_aw=1920&u_cd=24&u_sd=1&u_tz=-240&dmc=8&bc=31&nvt=1&uach=WyJMaW51eCIsIiIsIng4NiIsIiIsIjE0NS4wLjc2MzIuNiIsbnVsbCwwLG51bGwsIjY0IixbWyJOb3Q6QS1CcmFuZCIsIjk5LjAuMC4wIl0sWyJIZWFkbGVzc0Nocm9tZSIsIjE0NS4wLjc2MzIuNiJdLFsiQ2hyb21pdW0iLCIxNDUuMC43NjMyLjYiXV0sMF0.&url=https%3A%2F%2Fwww.nature.com%2Farticles%2Fs41586-026-10400-2&vis=1&psz=1904x119&msz=1904x0&fws=4&ohw=1920&dlt=1778608557023&idt=1237&prev_scp=type%3Darticle%26pos%3DLB1%26articleid%3Ds41586-026-10400-2%26doi%3D10.1038%252Fs41586-026-10400-2%26techmeta%3D119%2C123%2C128%2C129%2C132%2C140%2C144%26subjmeta%3D1135%2C1960%2C3923%2C400%2C639%2C766%26kwrd%3DHigh-harmonic%252Bgeneration%2CLaser-produced%252Bplasmas%26bpid%3D2000481557%2C8200828607%2C3005732355%26campaignID%3D20250910_3593%2C20250922_1656%2C20251212_0379%2C20260120_2314%2C20260321_3310%2C20260327_5522%2C20260327_5539%2C20260411_2060%2C20260415_7161%2C20260415_8041%2C20260429_9988%26logged%3Dn%26consent_analytics%3Dfalse%26consent_marketing%3Dfalse%26consent_ads%3Dfalse&adks=2050973735&frm=20
GET    document     https://9f59f11b370b802082d7128829cf6f5d.safeframe.googlesyndication.com/safeframe/1-0-45/html/container.html
GET    other        https://pagead2.googlesyndication.com/pagead/managed/dict/m202605120101/gpt
GET    script       https://ep2.adtrafficquality.google/sodar/sodar2.js
GET    document     https://ep2.adtrafficquality.google/sodar/sodar2/254/runner.html
GET    fetch        https://pagead2.googlesyndication.com/pcs/view?xai=AKAOjsuTx8JmVarDQTyoDzt6RFEuF4qC3xIG_Ki7MHcx8Rss2GvA1M4_G7rcRA11f8DZhGeKwhfc_JRIuVVK2siVU22EQV33K44OMd456sAg3Cu3J7ee53PJ3pUpVAFkTb_Jtk5rChlcrhzV_GkizobWQDL2fPfqDGzkeIGGNBqaI8kcCQAN3VsaJKI4m27FkiaLmc_18RCHlJFeTezlqpoUNcty9ObIp59AaXW8FSzSGvNO1eL2K8jTPLs_lxWZKdxuMJRGtuS0gfZBjzWWktuYXvGfaCAAqHgvi9EmPaUhrnwVc2Qw6U83OnKbjGHEdx49UMFQ70hDAjOiLYE3s0AJk8enk30swvS5uzLq6sRhJISr2d_MagYGqgnfcOmuk8-JAlw14FkFh_qov44tEQK_l9ZxNHK_a8iIKNrF1aPIAUIqCM1WxoWC4IJdufgn1H1sU48UFEQ&sig=Cg0ArKJSzOcwOqUFlFZMEAE&uach_m=%5BUACH%5D&adurl=
GET    script       https://pagead2.googlesyndication.com/pagead/js/r20260511/r20110914/client/window_focus.js
GET    script       https://pagead2.googlesyndication.com/pagead/managed/js/activeview/current/ufs_web_display.js
GET    image        https://tpc.googlesyndication.com/simgad/13753495253727463119
GET    script       https://pagead2.googlesyndication.com/bg/MK_n3_4yBb5PQspzm0gYhnuZeqyYa0O-dZQ5WGlSCLw.js
GET    fetch        https://pagead2.googlesyndication.com/pagead/gen_204?id=av-js&type=fle-fetch-start2
GET    fetch        https://pagead2.googlesyndication.com/pagead/gen_204?id=av-js&type=colleague-executed&name=4
GET    fetch        https://pagead2.googlesyndication.com/pagead/gen_204?id=av-js&type=reach&proto=CAlgAWACaAM%3D
GET    fetch        https://pagead2.googlesyndication.com/pcs/view?xai=AKAOjss3KkPewe4IWCed9_C0Mbufb4GRIcT65Qr-MCqO7ZxdyGP9Ct6_-mi_H1K0lHucv_jw1rWXFXo7MUVBtL_qm58KtrFJMRcDLLf0V57ouxJKs_8grY12M5owUXTy_uVyuGv2kuDUUfTgq6-wKsNuIOFBlfmKtMNWDRq_mOw1hXtXtz2GenaSn4cj0w0Pd2JJnB19IwE1IH0bBqBudx97-MoJVJKSuZvjDF376jGrQVW_yT9lIyLi54uAZBciBVhR4nGiZjoMLkLHR-zShLfjsnNybiE2FhlkERCVgpXYBAyaSSXXk-bvsg5BZx8pxmuEV3bole2RcIlJzeHGqtPbuTiQ5IKQ2UOL1KwWhD5rEguvp5LtREg66tUdswgADVfAp5zFtniCNkeLZYTcDF-NlvX_K1wY5DTrjrCKekDRLphfcylKwB38_w3jjK1G35tfToedYnIfQA&sig=Cg0ArKJSzFT9cXmpKto9EAE&uach_m=%5BUACH%5D&dett=2&adurl=
GET    image        https://ep2.adtrafficquality.google/generate_204?0038jg
GET    image        https://ep1.adtrafficquality.google/pagead/sodar?id=sodar2&v=254&t=2&li=gpt_m202605060101&jk=8045887748270486&bg=!y8ilyKrNAAYiAzjxAgM7AEcBe5WfOMuB32WgGyrviuAarp3CJSMKBNjg2G4gM9eMY5QXHU6-_L31IXWs1eJrnFaNvVVD4xqEDn_wTOWQulpGdqY_5QgFuAIAAAD5UgAAABZoAQd-ADZRNmlBcMjs3BCTkjrppw3XqDJjLY2tT3LqriMu6Xba2tfLBOHunmb6MmAzYzcHftHvJbKZFx0KADNiSt-tgp8s3KmI4PPoHqwmbDG9TIDnBN6jP_OCevSQ8PVixpmsCCxepSYgdagNsrBA63GZApNORPoXlSN4sFfbqq2fDcJfddYk46RLsn2el6ptWmCxBt_3vbT_dK4bnqCZvAO505CDN5t8P8Y9Lu1hxKNiHytfBKIvs5bxqEO9TpoFCqiBU6VArlZDBsudiqOaXGPuMGxKAmpAmQ4b6HtMM-mrzXehfumqWW2Z5r18huNA6NbW4pKf6tXKNyDHVpnaXGSNyPKZA9Kizo_HJwy9xIL1QpsFFNT0qSAlWIrJMVYPgaTFGn9yakBr1FGKzWBQu78HVfYUnGpr2H5LbCzhhxt2uPnrTyizT7bE2YkqDiXf7P1nJVyJtMH0a8Ev9ionAAwElJlWxtNwO0105xR4vCnigZB5CwiryfTxqGKpD221cMhNmUxsM1lsLO8EPGElLZ3HBPLHqaZqxX__glY1vip33gIhBYr5_7NM9G4JJtwXHxorlCqf1kfSbdBLMJAtESniNMH2oC-Xzzek9hia_sZLhgIucMyv_BN1J2BIxT57BhW5cHB_MTaYvOiPW8ypQ-J0DV0RUCVtcHa0gQEip_7Rs5Gl7p9WZ2qyeOwmrNfAcRxF9NWk-iPCuvBs6hNnz6zUMILsJTQfy_8D2aS79LZb-J4jxTVzSMtQhNiVqHo5i8_6LaYjE4el6d0JuL4adMZwzXX2M_0n7fEYFzBR60qM-VWmRU4v0F_h3zNLmuFRZJD8A2WQ5M0bjHT41Gmp8Hh-iNtZxlYR506XpDX9jYn8tYPXyT5DMqtb7loHY6WKnF-MxYDAcVJI8Ki_EZDK94iO1rIo0kDKS_08Ohsadt4r1VUZj6ml4EK5wG_a-fWS4a_9GhNXWpEZuHZL_N30SMh0RJoVWWGlczF_RcohPysgGsztl6RAhf4Cr3tIxPlvLtL_uPIgog
GET    fetch        https://pagead2.googlesyndication.com/pcs/activeview?xai=AKAOjst7BVH45C2dkEp39KJU11w0EXXbsSLti3m-lljgizpJosyqzsbABXmt5QX1C6BN9eojcyq7fxQ0txC1ut4OeH4hfp7oyN2HGV3yWGcsWgK6PpoTd7iO4wRDcBG_VCnWqpJ9fEBozfmlfCH4dA-P7XqlZJf5P5i4bY1rgY5KdPQOEDAEHi1CLqW2_TmLisM&sig=Cg0ArKJSzA2_XxYu05TyEAE&id=lidar2&mcvt=1001&p=45,596,135,1324&tm=1099.7000000476837&tu=99.20000004768372&mtos=1001,1001,1001,1001,1001&tos=1001,0,0,0,0&v=20260511&bin=7&avms=nio&bs=1920,1080&mc=1&vu=1&app=0&itpl=0&adk=2050973735&rs=4&la=0&cr=0&uach=WyJMaW51eCIsIiIsIng4NiIsIiIsIjE0NS4wLjc2MzIuNiIsbnVsbCwwLG51bGwsIjY0IixbWyJOb3Q6QS1CcmFuZCIsIjk5LjAuMC4wIl0sWyJIZWFkbGVzc0Nocm9tZSIsIjE0NS4wLjc2MzIuNiJdLFsiQ2hyb21pdW0iLCIxNDUuMC43NjMyLjYiXV0sMF0%3D&vs=4&r=v&co=7454135800&rst=1778608558584&rpt=366&isd=0&lsd=0&met=mue&wmsd=0&pbe=0&fle=0&vae=0&spb=0&sfl=0&ffslot=0&reach=8&io2=0
GET    fetch        https://pagead2.googlesyndication.com/pagead/gen_204?id=av-js&type=fle-fetch-later2
```

## 6. Detailed Responses Log

**200** https://idp.nature.com/exposed-details
- JSON size: 198 bytes

**200** https://www.nature.com/platform/contextual?doi=10.1038/s41586-026-10400-2
- JSON size: 593 bytes

**200** https://ep1.adtrafficquality.google/getconfig/sodar?sv=200&tid=gpt&tv=m202605060101&st=env&sjk=8045887748270486
- JSON size: 17950 bytes

**200** https://pagead2.googlesyndication.com/gampad/ads?pvsid=8045887748270486&correlator=3432333572225030&output=ldjh&gdfp_req=1&vrg=202605060101&ptt=17&impl=fifs&ltd=1&npa=1&iu_parts=270604982%2Cnature%2Carticles&enc_prev_ius=%2F0%2F1%2F1%2F2&prev_iu_szs=728x90&ifi=1&didk=4224914489&dids=div-gpt-ad-top-1&adfs=59990112&sfv=1-0-45&eri=1&sc=1&abxe=1&dt=1778608558400&lmt=1778608558&adxs=8&adys=46&biw=1920&bih=1080&scr_x=0&scr_y=0&btvi=0&ucis=1&oid=2&u_his=2&u_h=1080&u_w=1920&u_ah=1080&u_aw=1920&u_cd=24&u_sd=1&u_tz=-240&dmc=8&bc=31&nvt=1&uach=WyJMaW51eCIsIiIsIng4NiIsIiIsIjE0NS4wLjc2MzIuNiIsbnVsbCwwLG51bGwsIjY0IixbWyJOb3Q6QS1CcmFuZCIsIjk5LjAuMC4wIl0sWyJIZWFkbGVzc0Nocm9tZSIsIjE0NS4wLjc2MzIuNiJdLFsiQ2hyb21pdW0iLCIxNDUuMC43NjMyLjYiXV0sMF0.&url=https%3A%2F%2Fwww.nature.com%2Farticles%2Fs41586-026-10400-2&vis=1&psz=1904x119&msz=1904x0&fws=4&ohw=1920&dlt=1778608557023&idt=1237&prev_scp=type%3Darticle%26pos%3DLB1%26articleid%3Ds41586-026-10400-2%26doi%3D10.1038%252Fs41586-026-10400-2%26techmeta%3D119%2C123%2C128%2C129%2C132%2C140%2C144%26subjmeta%3D1135%2C1960%2C3923%2C400%2C639%2C766%26kwrd%3DHigh-harmonic%252Bgeneration%2CLaser-produced%252Bplasmas%26bpid%3D2000481557%2C8200828607%2C3005732355%26campaignID%3D20250910_3593%2C20250922_1656%2C20251212_0379%2C20260120_2314%2C20260321_3310%2C20260327_5522%2C20260327_5539%2C20260411_2060%2C20260415_7161%2C20260415_8041%2C20260429_9988%26logged%3Dn%26consent_analytics%3Dfalse%26consent_marketing%3Dfalse%26consent_ads%3Dfalse&adks=2050973735&frm=20

**200** https://pagead2.googlesyndication.com/pcs/activeview?xai=AKAOjst7BVH45C2dkEp39KJU11w0EXXbsSLti3m-lljgizpJosyqzsbABXmt5QX1C6BN9eojcyq7fxQ0txC1ut4OeH4hfp7oyN2HGV3yWGcsWgK6PpoTd7iO4wRDcBG_VCnWqpJ9fEBozfmlfCH4dA-P7XqlZJf5P5i4bY1rgY5KdPQOEDAEHi1CLqW2_TmLisM&sig=Cg0ArKJSzA2_XxYu05TyEAE&id=lidar2&mcvt=1001&p=45,596,135,1324&tm=1099.7000000476837&tu=99.20000004768372&mtos=1001,1001,1001,1001,1001&tos=1001,0,0,0,0&v=20260511&bin=7&avms=nio&bs=1920,1080&mc=1&vu=1&app=0&itpl=0&adk=2050973735&rs=4&la=0&cr=0&uach=WyJMaW51eCIsIiIsIng4NiIsIiIsIjE0NS4wLjc2MzIuNiIsbnVsbCwwLG51bGwsIjY0IixbWyJOb3Q6QS1CcmFuZCIsIjk5LjAuMC4wIl0sWyJIZWFkbGVzc0Nocm9tZSIsIjE0NS4wLjc2MzIuNiJdLFsiQ2hyb21pdW0iLCIxNDUuMC43NjMyLjYiXV0sMF0%3D&vs=4&r=v&co=7454135800&rst=1778608558584&rpt=366&isd=0&lsd=0&met=mue&wmsd=0&pbe=0&fle=0&vae=0&spb=0&sfl=0&ffslot=0&reach=8&io2=0

