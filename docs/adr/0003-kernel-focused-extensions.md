# Limit Extensions to Kernel capabilities

The Kernel accepts explicit Python Extension instances and lets them register tools, providers, SessionEntry types, and handlers for fixed input, context, provider, tool, lifecycle, and Session Hooks. Product-shell facilities such as directory discovery, Python entry-point loading, TUI rendering, shortcuts, themes, dynamic hot reload, and Cordis-style component lifecycles remain outside the Kernel; a future Host may discover Extensions before passing their instances to the Kernel.
