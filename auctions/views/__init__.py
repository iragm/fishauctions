"""Every view on the site, split by what part of it the view belongs to.

``views.py`` was a single 27,621-line module, which meant that finding a view meant grepping
for it and reading it meant a line number. The split is by *area*, along the seams the file
already had -- it was written in thematic runs, with each block of constants sitting just above
the views that use them, so almost every module below is one contiguous stretch of the original
file and `git log --follow` still works.

:mod:`auctions.views.base` holds the mixins and the permission helpers; every other module
imports from it and none of them import from each other in a circle. That property is checked:
the split was chosen so the module graph is acyclic, and it has to stay that way.

Names are re-exported here so ``from auctions import views`` and ``views.SomeView`` mean what
they always did -- ``urls.py`` refers to 381 of them that way. Import a *private* helper from
the module that defines it rather than from the package.
"""

from .account import *  # noqa: F403
from .admin_checklist import *  # noqa: F403
from .ajax import *  # noqa: F403
from .auction_admin import *  # noqa: F403
from .auction_extras import *  # noqa: F403
from .auction_pages import *  # noqa: F403
from .auction_stats import *  # noqa: F403
from .bap import *  # noqa: F403
from .base import *  # noqa: F403
from .browse import *  # noqa: F403
from .bulk_actions import *  # noqa: F403
from .bulk_add import *  # noqa: F403
from .bulk_add_lots import *  # noqa: F403
from .club_admin import *  # noqa: F403
from .club_api import *  # noqa: F403
from .club_api_keys import *  # noqa: F403
from .club_integrations import *  # noqa: F403
from .club_members import *  # noqa: F403
from .club_pages import *  # noqa: F403
from .club_reports import *  # noqa: F403
from .discord import *  # noqa: F403
from .embeds import *  # noqa: F403
from .exports import *  # noqa: F403
from .invoices import *  # noqa: F403
from .lot_pages import *  # noqa: F403
from .palette import *  # noqa: F403
from .payments import *  # noqa: F403
from .printing import *  # noqa: F403
from .selling import *  # noqa: F403
from .site_admin import *  # noqa: F403
from .site_pages import *  # noqa: F403
from .speakers import *  # noqa: F403
from .species import *  # noqa: F403
from .webhooks import *  # noqa: F403
