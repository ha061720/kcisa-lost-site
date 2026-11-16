import re, json, html
from bs4 import BeautifulSoup
from lxml import etree

def extract(xml_bytes):
    root=etree.fromstring(xml_bytes,parser=etree.XMLParser(recover=True,huge_tree=True))
    records=[]; notices=[]
    for n,entry in enumerate(root.findall('.//item')):
        title=entry.findtext('title') or ''
        description_node=entry.find('description')
        desc=''.join(etree.tostring(child,encoding='unicode',method='html') for child in description_node) if description_node is not None else ''
        soup=BeautifulSoup(desc,'html.parser')
        notices.append({'title':title,'registered':entry.findtext('regDate') or '', 'text':soup.get_text(' ',strip=True)[:350]})
        for table in soup.find_all('table'):
            headings=[x.get_text(' ',strip=True) for x in table.find_all('th')]
            if not any('품' in x for x in headings):continue
            section=table.find_previous(['h1','h2','h3'])
            center=section.get_text(' ',strip=True) if section else ''
            if not ('광장' in center or '센터' in center):
                previous=table.find_all_previous(['h1','h2','h3'])
                center=next((x.get_text(' ',strip=True) for x in previous if '안내센터' in x.get_text()),center)
            center=re.sub(r'\s+',' ',center)
            for tr in table.find_all('tr'):
                cells=[x.get_text(' ',strip=True) for x in tr.find_all('td',recursive=False)]
                if len(cells)<4:continue
                date,name,quantity,place=cells[:4]
                if not name or not re.search(r'20\d{2}',date):continue
                phone='02-2180-3566' if '만남의 광장' in center else ('02-2180-3564~5' if '평화의 광장' in center else '')
                records.append({'id':f'{n+1}-{len(records)+1}','date':date.replace('/','-'),'name':name,'quantity':quantity,'place':place,'feature':cells[4] if len(cells)>4 else '', 'note':cells[5] if len(cells)>5 else '', 'center':center,'phone':phone,'sourceTitle':title,'registered':entry.findtext('regDate') or ''})
    return {'records':records,'notices':notices,'sourceCount':len(root.findall('.//item'))}

if __name__=='__main__':
    from pathlib import Path
    raw=Path(__file__).with_name('source.xml').read_bytes()
    output=extract(raw)
    Path(__file__).with_name('data.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    print('공지',output['sourceCount'],'개, 개별 물품',len(output['records']),'개')
