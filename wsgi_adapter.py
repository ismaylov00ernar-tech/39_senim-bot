"""Expose the shared request handler as WSGI for the production Waitress server."""
import io
from email.message import Message
from http import HTTPStatus
from urllib.parse import quote


def application(handler):
    class Request(handler):
        def reply(self,status,body,content_type='text/html; charset=utf-8',headers=None):
            data=body.encode() if isinstance(body,str) else body
            self.result=(f'{status} {HTTPStatus(status).phrase}',[
                ('Content-Type',content_type),('Content-Length',str(len(data))),
                ('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),
                ('X-Frame-Options','DENY'),('Referrer-Policy','no-referrer'),
                ('Content-Security-Policy',"default-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'"),
                *(headers or {}).items()],[data])
    def app(environ,start_response):
        request=object.__new__(Request)
        request.path=quote(environ.get('PATH_INFO','/'),safe='/')+('?' + environ['QUERY_STRING'] if environ.get('QUERY_STRING') else '')
        request.headers=Message()
        for key,value in environ.items():
            if key.startswith('HTTP_'):request.headers[key[5:].replace('_','-')]=value
        request.headers['Content-Length']=environ.get('CONTENT_LENGTH','0') or '0'
        request.headers['Content-Type']=environ.get('CONTENT_TYPE','')
        request.rfile=environ.get('wsgi.input',io.BytesIO())
        request.client_address=(environ.get('REMOTE_ADDR',''),0)
        method=environ.get('REQUEST_METHOD','GET')
        try:
            if method in ['GET','HEAD']:request.do_GET()
            elif method=='POST':request.do_POST()
            else:request.reply(405,'Әдіс қолжетімсіз')
        except Exception:
            request.reply(500,'Сұрау орындалмады. Кейін қайталап көріңіз.')
        status,headers,body=request.result
        start_response(status,headers)
        return [] if method=='HEAD' else body
    return app
