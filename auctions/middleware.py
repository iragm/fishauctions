"""
Custom middleware for the auctions application.
"""


class CrossOriginIsolationMiddleware:
    """
    Middleware to add Cross-Origin-Opener-Policy and Cross-Origin-Embedder-Policy headers.
    
    These headers are required for SharedArrayBuffer and WebAssembly.Memory serialization,
    which are needed by the Vosklet voice recognition library.
    
    More info: https://web.dev/coop-coep/
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        
        # Add COOP header - allows popups while isolating the browsing context
        # Using 'same-origin' would be more secure but breaks OAuth popups
        response["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
        
        # Add COEP header - requires all resources to be loaded with CORS or same-origin
        response["Cross-Origin-Embedder-Policy"] = "require-corp"
        
        return response
