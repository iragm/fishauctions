import os
os.environ['SECRET_KEY'] = 'i(vtclg@m_fr@dr21#p_h-91)g(-!lg_j*wfg(heclpf*ny_bn'
os.environ['DEBUG'] = 'True'
os.environ['ALLOWED_HOSTS'] = 'auctions.toxotes.org'
os.environ['DATABASE_ENGINE'] = 'django.db.backends.mysql'
os.environ['DATABASE_NAME'] = 'auctions'
os.environ['DATABASE_USER'] = 'mysqluser'
os.environ['DATABASE_PASSWORD'] = 'supersecret'
os.environ['DATABASE_HOST'] = '127.0.0.1'
os.environ['DATABASE_PORT'] = '3306'
os.environ['BASE_URL'] = 'http://auctions.toxotes.org'
os.environ['EMAIL_USE_TLS'] = 'True'
os.environ['EMAIL_HOST'] = 'smtp.gmail.com'
os.environ['EMAIL_PORT'] = '587'
os.environ['EMAIL_HOST_USER'] = 'burlingtonfishclub@gmail.com'
os.environ['EMAIL_HOST_PASSWORD'] = 'gn5h4hztjk'
os.environ['DEFAULT_FROM_EMAIL'] = "Notifications"
os.environ['TIME_ZONE'] = 'UTC'
os.environ['SITE_DOMAIN'] = "toxotes.org"