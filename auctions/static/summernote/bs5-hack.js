// Function to replace data-toggle with data-bs-toggle
function replaceDataToggle() {
    document.querySelectorAll('[data-toggle="dropdown"]').forEach(function(el) {
      el.setAttribute('data-bs-toggle', 'dropdown');
      el.removeAttribute('data-toggle');
    });
  }

  // Run on page load
  document.addEventListener('DOMContentLoaded', function() {
    replaceDataToggle();

    // Observe for new elements added to the iframe's DOM
    const observer = new MutationObserver(function(mutations) {
      mutations.forEach(function(mutation) {
        if (mutation.addedNodes.length > 0) {
          replaceDataToggle(); // Replace in newly added elements
        }
      });
    });

    // Observe the document body for changes
    observer.observe(document.body, { childList: true, subtree: true });
  });
