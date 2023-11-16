"""
Brotab wrapper (prototype)
"""

from albert import *

import os
import tld
import requests
import shutil
import filetype
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor

from pathlib import Path
from hashlib import sha256

from typing import Tuple, List
from string import ascii_lowercase

from brotab.inout import is_port_accepting_connections
from brotab.inout import get_mediator_ports
from brotab.api import SingleMediatorAPI

from asyncio import new_event_loop, set_event_loop
from memoization import cached

from PIL import Image

md_iid = "2.0"
md_version = "1.0"
md_name = "Brotab"
md_description = "Control browser tabs"
md_license = "BSD-3"
md_url = "https://github.com/TexDash/albert_brotab"
md_bin_dependencies = ["brotab"]

class BrotabClient:
    """ Client to interact with Brotab Command line tool """

    current_clients = {}
    current_tabs = []
    icon_cache_dir = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'albert/brotab'
    icon_downloader_executor = ThreadPoolExecutor()
    icon_downloading_flags = {}

    def __init__(self):
        """ Constructor method """
        pass

    def parse_target_hosts(self, target_hosts: str):
        """
        Input: localhost:2000,127.0.0.1:3000
        Output: (['localhost', '127.0.0.1'], [2000, 3000])
        """
        hosts, ports = [], []
        for pair in target_hosts.split(','):
            host, port = pair.split(':')
            hosts.append(host)
            ports.append(int(port))
        return hosts, ports

    def update_clients(self, target_hosts=None):
        if target_hosts is None:
            ports = list(get_mediator_ports())
            hosts = ['localhost'] * len(ports)
        else:
            hosts, ports = self.parse_target_hosts(target_hosts)

        clients = [
            SingleMediatorAPI(prefix, host=host, port=port) for prefix, host, port in zip(ascii_lowercase, hosts, ports)
            if is_port_accepting_connections(port, host)
        ]

        self.current_clients = {}
        for clt in clients:
            # info(clt.__dict__)
            prefix = clt.__dict__["_prefix"][:-1]  # get rid of the suffix '.'
            self.current_clients[prefix] = {
                'browser': clt.__dict__["_browser"],
                "api": clt
            }
        
        return self.current_clients
    
    @cached(ttl=10)
    def is_installed(self):
        """ Checks if Brotab is installed """
        path = shutil.which('brotab')
        if path is None:
            return False
        return True

    @cached(ttl=2)
    def fetch_tabs(self):
        """ Index Tabs list """
        self.update_clients()

        loop = new_event_loop()
        set_event_loop(loop)
        tabs_listed = self.return_tabs()

        self.current_tabs = []
        for tab in tabs_listed:
            tab_id, title, url = tab.split("\t")

            prefix = tab_id.split('.')[0]
            browser_name = self.current_clients[prefix]['browser']

            # check if url is valid
            url_tld_obj = tld.get_tld(url, as_object=True, fail_silently=True)
            if url_tld_obj:
                netloc = url_tld_obj.parsed_url.netloc
                url_domain_list = get_url_domain_list(url_tld_obj)
            else:
                netloc = None
                url_domain_list = [None]

            icon = None
            if netloc:
                # first try to get icon from local
                icon = get_icon_from_local(self.icon_cache_dir, url_domain_list)

                # trigger async downloader (for next event)
                if icon is None and netloc not in self.icon_downloading_flags:
                    self.icon_downloading_flags[netloc] = "downloading"
                    future = self.icon_downloader_executor.submit(
                        download_icon, netloc, url_domain_list,
                        self.icon_cache_dir, self.icon_downloading_flags
                    )
                    future.add_done_callback(clear_icon_downloading_flags)

            # info(netloc)
            # info(self.icon_downloading_flags)

            temp_state_flag = (netloc and icon is None) # valid netloc but untouched icon

            if 'firefox' in browser_name:
                title_suffix = ' - Mozilla Firefox'
                if icon in [None, "requested_but_unavailable"]:
                    icon = "xdg:firefox"
            elif 'chrome' in browser_name:
                title_suffix = ' - Google Chrome'
                if icon in [None, "requested_but_unavailable"]:
                    icon = "xdg:google-chrome"
            else:
                title_suffix = ' - Browser'
                if icon in [None, "requested_but_unavailable"]:
                    icon = "xdg:browser"

            new_title = title + title_suffix

            # avoid icon cache in albert
            temp_state_str = "__temp_state_flag__" if temp_state_flag else ""
            albert_id = sha256((new_title + temp_state_str).lower().encode('utf-8')).hexdigest()

            self.current_tabs.append({
                "albert_id": albert_id,
                "tab_id": tab_id,
                "title": new_title,
                "url": url,
                "domain": url_domain_list[-1],
                "browser": browser_name,
                "icon_url": str(icon)
            })

        return self.current_tabs
    
    def search_tabs(self, filter_term=None):
        """ Returns a list of tabs, optionally filtered by the filter_query parameter """

        allTabs = self.fetch_tabs()
        if not filter_term:
            return allTabs

        tabs = []
        for tab in allTabs:
            if filter_term.lower() in tab["title"].lower() or filter_term.lower() in tab["url"].lower():
                tabs.append(tab)

        return tabs

    def return_tabs(self):
        tabs = []
        for prefix in self.current_clients:
            tabs += self.current_clients[prefix]['api'].list_tabs([])
        return tabs
    
    def activate_tab(self, tab_id):
        prefix = tab_id.split('.')[0]
        if prefix in self.current_clients:
            self.current_clients[prefix]['api'].activate_tab([tab_id], True)

    def close_tab(self, tab_id):
        # Try stdin if arguments are empty
        prefix = tab_id.split('.')[0]
        if prefix in self.current_clients:
            self.current_clients[prefix]['api'].close_tabs([tab_id])

    def close_tabs_by_title(self, tab_title):
        for tab in self.current_tabs:
            if tab['title'] == tab_title:
                self.close_tab(tab['tab_id'])

    def close_tabs_by_domain(self, domain):
        for tab in self.current_tabs:
            if tab['domain'] == domain:
                self.close_tab(tab['tab_id'])

    def close_tabs_by_browser(self, browser):
        for tab in self.current_tabs:
            if tab['browser'] == browser:
                self.close_tab(tab['tab_id'])

def get_icon_from_local(icon_cache_dir, url_domain_list):
    icon = None
    for url_domain in url_domain_list:
        loc = get_cache_location(icon_cache_dir, url_domain)
        if loc.exists():
            if filetype.is_image(loc):
                icon = f"file:{str(loc)}"
                break
            else:
                icon = "requested_but_unavailable"
    return icon

def download_icon(netloc, url_domain_list, icon_cache_dir, icon_downloading_flags):
    # scan the list containing possible url domains
    loc = None
    for url_domain in url_domain_list:
        loc = get_cache_location(icon_cache_dir, url_domain)

        if not icon_cache_dir.exists():
            icon_cache_dir.mkdir(parents=True, exist_ok=True)

        if not loc.exists():
            favicon_url = get_favicon_url(url_domain)
            # subprocess.run(["wget", "--no-use-server-timestamps", "-q", "-O", loc, favicon_url])
            # info(f"requesting {favicon_url}")
            res = requests.get(favicon_url, allow_redirects=True, timeout=2)
            if res:
                image_data = BytesIO(res.content)
                with Image.open(image_data) as image:
                    width, height = image.size
                    # info(f"requested image resolution: {width}x{height}")
                    if width < 32 or height < 32:  # minimum size 32x32
                        image = image.resize((32, 32))
                    image.save(loc, format="png")
            else:
                # info("requesting failed")
                # can not get the icon, write a placeholder file containing the domain
                with open(loc, "w") as text_file:
                    text_file.write(f"{url_domain.lower()}\n")

        if filetype.is_image(loc):  # got a valid image, break the for loop
            break
    
    if loc is not None and filetype.is_image(loc):
        return icon_downloading_flags, netloc, str(loc)
    else:
        return icon_downloading_flags, netloc, ""

def get_favicon_url(url_domain):
    # favicon_url = f'https://icons.duckduckgo.com/ip3/{url_domain}.ico'
    favicon_url = f'http://www.google.com/s2/favicons?domain={url_domain}&sz=32'
    return favicon_url

def get_url_domain_list(url_tld_obj):
    url_domain = url_tld_obj.fld
    if len(url_tld_obj.subdomain) > 0:
        url_subdomain = url_tld_obj.subdomain.split('.')[-1] + '.' + url_domain
    else:
        url_subdomain = url_domain
    return [url_subdomain, url_domain]

def get_cache_location(icon_cache_dir, url_domain):
    return icon_cache_dir / sha256(url_domain.lower().encode('utf-8')).hexdigest()

def clear_icon_downloading_flags(future):
    icon_downloading_flags, netloc, iconloc = future.result()
    assert(netloc in icon_downloading_flags)
    del icon_downloading_flags[netloc]


class Plugin(PluginInstance, GlobalQueryHandler):
    def __init__(self):
        GlobalQueryHandler.__init__(self,
                                     id=md_id,
                                     name=md_name,
                                     description=md_description,
                                     synopsis="<brotab filter>",
                                     defaultTrigger='b ')
        PluginInstance.__init__(self, extensions=[self])

        self.brotab_client = BrotabClient()

    def handleGlobalQuery(self, query):
        if not self.brotab_client.is_installed():
            critical("Brotab is not installed on your system.")
            return []
        
        rank_items = []
        user_query = query.string.strip().lower()
        # info(user_query)

        tabs = self.brotab_client.search_tabs(user_query)
        # info(tabs)

        for tab in tabs:
            short_tab_title = tab['title'][:15]
            if short_tab_title != tab['title']:
                short_tab_title += ' ...'

            rank_items.append(RankItem(
                item=StandardItem(
                    id=tab['albert_id'],
                    text= tab['title'],
                    subtext=tab['url'],
                    inputActionText=tab['title'],
                    iconUrls=[tab['icon_url']],
                    actions=[
                        Action("activate_tab", "Activate tab: %s" % short_tab_title, lambda t=tab: self.brotab_client.activate_tab(t['tab_id'])),
                        Action("close_tabs_by_title", "Close all tabs with title: %s" % short_tab_title, lambda t=tab: self.brotab_client.close_tabs_by_title(t['title'])),
                        Action("close_tabs_by_domain", "Close all tabs with domain: %s" % tab['domain'], lambda t=tab: self.brotab_client.close_tabs_by_domain(t['domain'])),
                        Action("close_tabs_by_browser", "Close all tabs with browser: %s" % tab['browser'], lambda t=tab: self.brotab_client.close_tabs_by_browser(t['browser'])),
                    ]
                ),
                score=0
            ))

        return rank_items
