#!/usr/bin/env python3
import urllib.request
from urllib.parse import urlparse
from contextlib import closing
import shutil
import re
import os
import json
import requests
from requests.exceptions import SSLError
from requests import exceptions
from bs4 import BeautifulSoup
from mimetypes import guess_extension
import textract
from zipfile import ZipFile, BadZipfile
import io
import logging

logger = logging.getLogger(__name__)

class FboAttachments():
    '''
    Given the json of a nightly fbo file, retrieve all of its attachments from the fbo url, 
    extract the text, and insert those details as new key:value pairs into the json.

    Parameters:
        nightly_data (json or dict): the json/dict representation of a nightly FBO file
    '''

    def __init__(self, nightly_data):
        self.nightly_data = nightly_data


    @staticmethod
    def get_divs(fbo_url):
        '''
        gets the `notice_attachment_ro` divs within given fbo url

        Parameters:
            fbo_url (str): a url to an fbo notice.

        Returns:
            attachment_divs (list): a list of each html div with its text
        '''

        try:
            #generous timeout for gov sites
            r = requests.get(fbo_url, timeout = 300)
        except Exception as e:
            logger.error(f"Exception occurred getting attachment divs from {fbo_url}:  \
                            {e}", exc_info=True)
            attachment_divs = []
            return attachment_divs
        r_content = r.content
        soup = BeautifulSoup(r_content, "html.parser")
        attachment_divs = soup.find_all('div', {"class": "notice_attachment_ro"})

        return attachment_divs


    @staticmethod
    def get_attachment_text(file_name, url):
        '''
        Extract text from a file.

        Arguments:
            file (str): the path to a file.
            url  (str): the url of file (for logging purposes)

        Returns:
            text (str): a string representing the text of the file.
        '''
        try:
            b_text = textract.process(file_name, encoding='utf-8', errors = 'ignore')
        #TypeError is raised when None is passed to str.decode()
        #This happens when textract can't extract text from scanned documents
        except textract.exceptions.MissingFileError as e:
            b_text = None
            logger.error(f"Couldn't textract {file_name} from {url} since the file couldn't be found:  \
                           {e}", exc_info=True)
        except TypeError:
            b_text = None
        except Exception as e:
            logger.error(f"Exception occurred textracting {file_name} from {url}:  \
                           {e}", exc_info=True)
            b_text = None
        if b_text:
            text = b_text.decode('utf8', errors = 'ignore')
        else:
            text = ''
        text = text.strip()

        return text


    @staticmethod
    def insert_attachments(file_list, notice):
        '''
        Inserts each attachment's url and text into the nightly json file. Also inserts
        placeholders for the prediction, decision_boundary, and validation.

        Parameters:
            file_list (list): a list of tuples containing attachment file paths and urls
            notice (dict): a dict representing a single fbo notice

        Returns:
            notice (dict): a dict representing a single fbo notice with attachment insertions made
        '''
        
        notice['attachments'] = []
        for file_url_tup in file_list:
            file_name, url = file_url_tup
            if file_name:
                text = FboAttachments.get_attachment_text(file_name, url)
                if text:
                    attachment_dict = {'machine_readable':True,
                                    'text':text, 
                                    'url':url,
                                    'prediction':None, 
                                    'decision_boundary':None,
                                    'validation':None,
                                    'trained':False}
                else:#empty strings are falsy
                    attachment_dict = {'machine_readable':False,
                                       'text':None, 
                                       'url':url,
                                       'prediction':None, 
                                       'decision_boundary':None,
                                       'validation':None,
                                       'trained':False}
            else:
                attachment_dict = {'machine_readable':False,
                                   'text':None, 
                                   'url':url,
                                   'prediction':None, 
                                   'decision_boundary':None,
                                   'validation':None,
                                   'trained':None}
            notice['attachments'].append(attachment_dict)

        return notice

    
    @staticmethod
    def size_check(url):
        """
        Does the url contain a resource that's less than 500mb?

        Arguments:
            url (str): an attachment url that's passable `requests.head()`

        Returns:
            bool: True if resource < 500mb
        """
        try:
            #generous timeout for gov sites
            h = requests.head(url, timeout = 300)
        except Exception as e:
            logger.error(f"Exception occurred getting file size with HEAD request from {url}. \
                           This means the file wasn't downloaded:  \
                           {e}", exc_info=True)
            return False
        if h.status_code not in [200, 302]:
            logger.error(f"Non-200/302 status code ({h.status_code}) getting file size with HEAD request from {url}. \
                            This means the file wasn't downloaded.")
            return False
        elif h.status_code == 302:
            redirect_header = h.headers
            redirect_url = redirect_header['Location']
            if 'http' not in redirect_url:
                parsed_url = urlparse(url)
                url_domain = '{url.scheme}://{url.netloc}'.format(url=parsed_url)
                redirect_url = url_domain + redirect_url
            try:
                #generous timeout for gov sites
                h = requests.head(redirect_url, timeout = 300)
            except Exception as e:
                logger.error(f"Exception occurred getting file size with redirected HEAD request from {url}:  \
                                {e}", exc_info=True)
                return False
        header = h.headers
        content_length = header.get('content-length', None)
        if content_length and int(content_length) > 5e8:  # 500 mb approx
            return False
        elif not content_length:
            return False
        else:
            return True
    

    @staticmethod
    def get_filename_from_cd(cd):
        """
        Get filename from content-disposition

        Arguments:
            cd: the content-disposition returned in the headers by requests.get()

        Returns:
            file_name (str) or None
        """
        
        if not cd:
            return None
        file_name = re.findall('filename=(.+)', cd)
        if len(file_name) == 0:
            return None
        file_name = file_name[0].strip('\"')
        
        return file_name

    
    @staticmethod
    def get_file_name(attachment_url, content_type):
            '''
            Get filename using some heuristics if get_filename_from_cd() failed.
            Arguments:
                attachment_url (str): the url for a fbo notice attachment
                content_type (str): the content-type as found in a requests response header

            Returns:
                file_name (str): a string for the file's name
            '''
            
            file_name = os.path.basename(attachment_url)
            extensions = ['.csv','.docx','.doc','.eml', '.epub', '.gif', '.html', '.jpeg', '.htm',
                          '.jpg', '.json', '.log', '.mp3', '.msg', '.odt', '.ogg', '.pdf', '.png', '.pptx',
                          '.ps', '.psv', '.rtf', '.tff', '.tiff', '.tsv', '.txt', '.wav', '.xlsx', '.xls']
            extensions_re = re.compile(r"|".join(extensions))
            matches = extensions_re.findall(file_name)
            if matches:
                for m in matches:
                    file_name = file_name.replace(m,'')
                extension = max(matches, key=len)
                file_name = file_name+extension
            else:
                if content_type == 'application/zip':
                    extension = '.zip'
                elif content_type == 'application/msword':
                    extension = '.rtf'
                else:
                    if content_type:
                        extension = guess_extension(content_type.split()[0].rstrip(";"))
                    else:
                        extension = None
                if not extension:
                    extension = '.txt'
                file_name = file_name + extension
            
            return file_name
    
    
    @staticmethod
    def get_neco_navy_mil_attachment_urls(attachment_href):
        '''
        Scrape the attachment urls from the https://www.neco.navy.mil/... page

        Arguments:
            attachment_href (str): the url, e.g. https://www.neco.navy.mil/.....

        Returns:
            attachment_urls (list): a list of the attachment urls scraped from the 
            dwnld2_row id of the html table
        '''
        
        try:
            #generous timeout for gov sites
            r = requests.get(attachment_href, timeout = 300)
        except Exception as e:
            logger.error(f"Exception occurred making GET request to {attachment_href}:  \
                            {e}", exc_info=True)
            attachment_urls = []
            return attachment_urls
        r_content = r.content
        soup = BeautifulSoup(r_content, "html.parser")
        attachment_id_re = re.compile(r'(dwnld\d_row)')
        attachment_rows = soup.findAll("tr", {"id": attachment_id_re})
        attachment_urls = []
        for row in attachment_rows:
            file_path = row.find('a')['href']
            if 'https://www.neco.navy.mil' not in file_path:
                attachment_url = f'https://www.neco.navy.mil{file_path}'
            else:
                attachment_url = file_path
            attachment_urls.append(attachment_url)
        
        return attachment_urls


    @staticmethod
    def get_attachment_url_from_div(div):
        '''
        Extract the attachment url from the href attribute of the attachmen div's anchor tag

        Arguments:
            div (an element within the bs4 object returned by soup.find_all())

        Returns:
            attachment_url (list): a list of the attachment urls as strings 
        '''
        attachment_href = div.find('a')['href'].strip()
        #some href's oddly look like: 'http://  https://www....'
        attachment_href = max(attachment_href.split(), key=len)
        if '/utils/view?id' in attachment_href:
            attachment_url = 'https://www.fbo.gov'+attachment_href
        elif 'neco.navy.mil' in attachment_href:
            #this returns a list
            attachment_urls = FboAttachments.get_neco_navy_mil_attachment_urls(attachment_href)
            return attachment_urls, True
        else:
            attachment_url = attachment_href
        attachment_urls = [attachment_url]
        
        return attachment_urls, False
    
    
    @staticmethod
    def get_and_write_attachment_from_ftp(attachment_url, out_path, textract_extensions):
        '''
        Get and write a file from a FTP

        Arguments:
            attachment_url (str): the ftp url of the attachment
            out_path (str): the directory to which you'd like to write the attachment
            textract_extensions (tup): a tuple of file extensions

        Returns:
            file_out_path (str): the relative file path to which the ftp file was written
        '''

        file_name = os.path.basename(attachment_url)
        file_out_path = os.path.join(out_path, file_name)
        if file_out_path.endswith(textract_extensions):
            try:
                #generous timeout for gov sites
                with closing(urllib.request.urlopen(attachment_url, timeout=300)) as ftp_r:
                    with open(file_out_path, 'wb') as f:
                        shutil.copyfileobj(ftp_r, f)
            except Exception as e:
                logger.error(f"Exception occurred downloading FTP attachment from {attachment_url}:  \
                                {e}", exc_info=True)
                    
        return file_out_path

    @staticmethod
    def write_attachments(attachment_divs):
        '''
        Given a list of the attachment_divs from an fbo notice's url, write each file's contents
        and return a list of all of the files written.

        Parameters:
            attachment_divs (list): a list of attachment_divs. Returned by FboAttachments.get_divs()

        Returns:
            file_list (list): a list of tuples containing files paths and urls of each file that has been written
        '''

        textract_extensions = ('.doc', '.docx', '.epub', '.gif', '.htm', 
                               '.html','.odt', '.pdf', '.rtf', '.txt')
        cwd = os.getcwd()
        if 'fbo-scraper' in cwd:
            i = cwd.find('fbo-scraper')
            root_path = cwd[:i+len('fbo-scraper')]
        else:
            i = cwd.find('root')
            root_path = cwd
        attachments_dir = 'attachments'
        out_path = os.path.join(root_path, attachments_dir) 
        if not os.path.exists(out_path):
            os.makedirs(out_path)
        file_list = []
        for div in attachment_divs:
            try:
                attachment_urls, is_neco_navy_mil = FboAttachments.get_attachment_url_from_div(div)
                for attachment_url in attachment_urls:
                    #some are ftp and we can get the file now
                    if 'ftp://' in attachment_url:
                        file_out_path = FboAttachments.get_and_write_attachment_from_ftp(attachment_url,
                                                                                         out_path,
                                                                                         textract_extensions)
                        file_list.append((file_out_path, attachment_url))                                                                                        
                    else:
                        file_smaller_than_500mb = FboAttachments.size_check(attachment_url)
                        if file_smaller_than_500mb:
                            try:
                                #generous timeout for gov sites
                                r = requests.get(attachment_url, timeout=300)
                            except Exception as e:
                                logger.error(f"Exception occurred making GET request for an attachment to {attachment_url}. \
                                               This means we didn't download it:  {e}", exc_info=True)
                                #capturing as non-machine readable attachment
                                file_list.append((None, attachment_url)) 
                                continue
                            if r.status_code == 302:
                                redirect_header = r.headers
                                redirect_url = redirect_header['Location']
                                try:
                                    #generous timeout for gov sites
                                    r = requests.get(redirect_url, timeout=300)
                                except Exception as e:
                                    logger.error(f"Exception occurred making GET request for an attachment after a redirect to {attachment_url}. \
                                                   This means we didn't download it:  {e}", exc_info=True)
                                    #capturing as non-machine readable attachment
                                    file_list.append((None, attachment_url)) 
                                    continue
                            content_disposition = r.headers.get('Content-Disposition', None)
                            file_name = FboAttachments.get_filename_from_cd(content_disposition)
                            if not file_name:
                                content_type = r.headers.get('Content-Type', None)
                                file_name = FboAttachments.get_file_name(attachment_url, content_type)
                            if '.zip' in file_name:
                                z = ZipFile(io.BytesIO(r.content))
                                z.extractall(out_path)
                                zip_file_list = z.filelist
                                for zip_file in zip_file_list:
                                    try:
                                        zip_filename = zip_file.filename
                                        if not zip_filename.endswith('/'):
                                            file_out_path = os.path.join(out_path,
                                                                         zip_filename)
                                            if file_out_path.endswith(textract_extensions):
                                                file_list.append((file_out_path, attachment_url))
                                            else:
                                                #capturing as non-machine readable attachment
                                                file_list.append((None, attachment_url))
                                    except AttributeError:
                                        pass 
                            else:
                                file_out_path = os.path.join(out_path,file_name).replace('"','')
                                if file_out_path.endswith(textract_extensions):
                                    with open(file_out_path, 'wb') as f:
                                        f.write(r.content)
                                    file_list.append((file_out_path, attachment_url))
                                else:
                                    #capturing as non-machine readable attachment
                                    file_list.append((None, attachment_url)) 
                        else:
                            #capturing as non-machine readable attachment
                            file_list.append((None, attachment_url)) 
            except Exception as e:
                logger.error(f"Exception extracting attachment url from div: {div} \
                               This means we didn't get the file:  {e}", exc_info=True)
                continue
            if is_neco_navy_mil:
                #if the div was from a neco.navy.mil solicitation, we don't need to hit all the urls
                #since they're duplicates
                break
        return file_list
    
    def update_nightly_data(self):
        '''
        Given the json of a nightly fbo file, retrieve all of its attachments from the fbo url, 
        extract the text, and insert those details as new key:value pair(s) into the json.

        Returns:
            updated_nightly_data (dict): a dict representing a nightly file with attachment urls
            and attachment text inserted as new key:value pairs.
        '''
        nightly_data = self.nightly_data
        file_lists = []
        for k in nightly_data:
            for i, notice in enumerate(nightly_data[k]):
                try:
                    fbo_url = notice['url']
                except:
                    continue
                attachment_divs = FboAttachments.get_divs(fbo_url)
                file_list = FboAttachments.write_attachments(attachment_divs)
                file_lists.append(file_list)
                updated_notice = FboAttachments.insert_attachments(file_list, notice)
                nightly_data[k][i] = updated_notice
        updated_nightly_data = nightly_data
        #cleanup
        cwd = os.getcwd()
        attachments_dir = 'attachments'
        attachments_path = os.path.join(cwd, attachments_dir) 
        try: 
            shutil.rmtree(attachments_path)
        except FileNotFoundError:
            pass
        
        return updated_nightly_data
