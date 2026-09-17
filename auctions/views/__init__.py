"""Every view on the site, split by the part of it the view belongs to.

``views.py`` was one 27,621-line module, so finding a view meant grepping and reading it meant a
line number. The split follows the seams the file already had -- it was written in thematic runs --
so almost every module here is one contiguous stretch of the original.

``git log --follow`` does not carry that history: one file becoming 34 is not a rename. Ask git
about the content instead -- ``git log -S'class SquareConnectView'`` finds every commit that touched
a view wherever it lived, and ``git log -- auctions/views.py`` still has all 1,036 commits.

:mod:`auctions.views.base` holds the mixins and permission helpers; every other module imports from
it and none import from each other in a circle, which is checked.

Names are re-exported here so ``views.SomeView`` still works -- ``urls.py`` refers to 347 of them
that way. Import a *private* helper from the module that defines it.
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
from .club_finder import *  # noqa: F403
from .club_integrations import *  # noqa: F403
from .club_members import *  # noqa: F403
from .club_pages import *  # noqa: F403
from .club_reports import *  # noqa: F403
from .discord import *  # noqa: F403
from .embeds import *  # noqa: F403
from .exports import *  # noqa: F403
from .invoices import *  # noqa: F403
from .lot_pages import *  # noqa: F403
from .moderation import *  # noqa: F403
from .palette import *  # noqa: F403
from .payments import *  # noqa: F403
from .printing import *  # noqa: F403
from .selling import *  # noqa: F403
from .site_admin import *  # noqa: F403
from .site_pages import *  # noqa: F403
from .speakers import *  # noqa: F403
from .species import *  # noqa: F403
from .usability import *  # noqa: F403
from .webhooks import *  # noqa: F403
