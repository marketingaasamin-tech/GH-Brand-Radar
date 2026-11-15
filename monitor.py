"""GH four-brand Google News RSS index monitor. Python 3.10+; stdlib only."""
import csv, datetime as dt, email.utils, hashlib, html, json, os, pathlib, re, sys, urllib.request, urllib.parse, xml.etree.ElementTree as ET

BASE = pathlib.Path(__file__).parent
CONFIG = json.loads((BASE / 'config.json').read_text(encoding='utf8'))
STATE_FILE = BASE / 'state.json'
LOG_FILE = BASE / 'mentions.csv'
HEADERS = ['discovered_utc', 'published_utc', 'brand', 'channel', 'priority', 'title', 'url', 'source', 'status']


def norm(s):
    s = html.unescape(s or '').replace('\u200c', ' ').replace('ي', 'ی').replace('ك', 'ک')
    s = re.sub('<[^>]*>', ' ', s)
    return re.sub(r'\s+', ' ', s).strip().lower()


def find_brand(text):
    t=norm(text)
    return [brand for brand, variants in CONFIG['brands'].items() if any(norm(v) in t for v in variants)]


def priority(text):
    t=norm(text)
    if any(norm(k) in t for k in CONFIG['priority_keywords']): return 'فوری - بررسی انسانی'
    if any(norm(k) in t for k in CONFIG['opportunity_keywords']): return 'نیازمند پاسخ / فرصت'
    return 'اطلاع‌رسانی'


def fetch_feed(url):
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (compatible; GHBrandRadar/1.0)'})
    with urllib.request.urlopen(req, timeout=18) as response:
        data=response.read(2_000_000)
    root=ET.fromstring(data)
    items=[]
    for item in root.findall('.//channel/item'):
        get=lambda tag: (item.findtext(tag) or '').strip()
        title=html.unescape(re.sub('<[^>]*>',' ',get('title')))
        source_el=item.find('source')
        source=source_el.text if source_el is not None and source_el.text else ''
        pub=get('pubDate')
        try: pub=email.utils.parsedate_to_datetime(pub).astimezone(dt.timezone.utc).isoformat()
        except (ValueError,TypeError,IndexError): pub=''
        items.append({'title':title,'url':get('link'),'source':source,'published_utc':pub})
    return items


def post_telegram(chat_id,token,message):
    url=f'https://api.telegram.org/bot{token}/sendMessage'
    data=urllib.parse.urlencode({'chat_id':chat_id,'text':message,'disable_web_page_preview':'true'}).encode()
    with urllib.request.urlopen(urllib.request.Request(url,data=data,method='POST'),timeout=15) as response:
        return response.status


def run():
    state=json.loads(STATE_FILE.read_text(encoding='utf8')) if STATE_FILE.exists() else {'initialized':False,'seen':[]}
    seen=set(state.get('seen',[]))
    now=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    found=[]; errors=[]; successful=0
    for feed in CONFIG['search_sources']:
        try: items=fetch_feed(feed['url']); successful+=1
        except Exception as ex:
            errors.append(f"{feed['brand']} / {feed['channel']}: {type(ex).__name__}: {ex}")
            continue
        for item in items:
            matches=find_brand(item['title'])
            if feed['brand'] not in matches: continue
            source_url=urllib.parse.urlparse(item['url']).netloc.lower()
            if CONFIG.get('ignore_owned_domains') and any(d in source_url for d in CONFIG['exclude_domains']): continue
            # Deduplicate same article from different indexed searches, and also across runs
            unique=hashlib.sha256((norm(item['title'])+'|'+norm(item['source'])).encode()).hexdigest()
            if unique in seen: continue
            seen.add(unique)
            found.append({'discovered_utc':now,'published_utc':item['published_utc'],'brand':feed['brand'],
                          'channel':feed['channel'],'priority':priority(item['title']), 'title':item['title'],
                          'url':item['url'],'source':item['source'],'status':'جدید'})
    # Do not mark baseline complete when all feeds fail
    if successful == 0:
        print('All feeds failed; state unchanged.',file=sys.stderr)
        print('\n'.join(errors),file=sys.stderr)
        raise SystemExit(1)
    baseline=not state.get('initialized',False) and CONFIG.get('startup_mode')=='baseline'
    if baseline:
        print(f'Initialized baseline with {len(found)} discovered items; no alerts sent.')
        found=[]
    if found:
        exists=LOG_FILE.exists()
        with LOG_FILE.open('a',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=HEADERS)
            if not exists or LOG_FILE.stat().st_size==0: writer.writeheader()
            writer.writerows(found)
    token=os.environ.get('GH_TELEGRAM_BOT_TOKEN','').strip()
    chat=os.environ.get('GH_TELEGRAM_CHAT_ID','').strip()
    if found and token and chat:
        critical=[x for x in found if x['priority'] == 'فوری - بررسی انسانی']
        other=[x for x in found if x['priority'] != 'فوری - بررسی انسانی']
        to_send=(critical+other)[:CONFIG.get('telegram_max_per_run',12)]
        for row in to_send:
            body=f"رادار برند | {row['brand']}\n{row['priority']}\n{row['channel']}\n{row['title']}\n{row['url']}"
            try: post_telegram(chat,token,body[:4000])
            except Exception as ex: print(f'Telegram alert failed: {ex}',file=sys.stderr)
        if len(found)>len(to_send):
            try: post_telegram(chat,token,f'رادار برند: {len(found)-len(to_send)} مورد جدید دیگر در فایل mentions.csv ثبت شد.')
            except Exception as ex: print(f'Telegram summary failed: {ex}',file=sys.stderr)
    elif found:
        print('Telegram notifications disabled: set GH_TELEGRAM_BOT_TOKEN and GH_TELEGRAM_CHAT_ID.')
    # keep max 50000 entries; duplicate events can reappear beyond this window
    STATE_FILE.write_text(json.dumps({'initialized':True,'seen':list(seen)[-50000:]},ensure_ascii=False,indent=2),encoding='utf8')
    print(f'Feeds OK: {successful}/{len(CONFIG["search_sources"])}; new: {len(found)}; errors: {len(errors)}')
    if errors: print('\n'.join(errors[:12]),file=sys.stderr)

if __name__=='__main__': run()
