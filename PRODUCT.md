# Product

## Register

product

## Users

PulseGuard is used by developers, operators, and small SRE teams who need a local or LAN console for UI and API probes. They are usually checking task health, editing probe definitions, inspecting failures, and rerunning checks during operations work.

## Product Purpose

PulseGuard helps users define UI/API checks, run them on demand or on schedule, inspect artifacts, and configure alert delivery. Success means the console feels like a reliable operations tool: dense enough for repeated work, quiet enough for long sessions, and explicit about state.

## Supported Console Viewports

The operations console targets desktop and laptop browsers. Phone viewport compatibility is explicitly out of scope: no phone-specific navigation, layout, or visual acceptance is required. Existing narrow-screen CSS is not a phone-support commitment.

This boundary applies only to the console. UI probes continue to support H5 page mode and configured mobile target viewports; probe capabilities must not be removed or restricted by this console policy.

## Brand Personality

Calm, technical, exact. The interface should feel native, durable, and operational rather than decorative or promotional.

## Anti-references

- AI generated dashboard templates with generic Inter everywhere.
- Decorative hero panels, eyebrow labels, glass effects, gradient surfaces, and filler badges.
- Flat type hierarchy where labels, content, titles, and metrics sit too close together.
- Bounce or elastic easing, especially `cubic-bezier(0.12, 0.4, 0.29, 1.46)`.
- Layout property animation such as `transition: width`.

## Design Principles

1. Make state obvious before making the page visually interesting.
2. Treat tables, forms, drawers, and evidence panels as first-class work surfaces.
3. Use familiar Ant Design controls, but constrain them with PulseGuard tokens.
4. Prefer native, fast, predictable typography over fashionable web-font identity.
5. Motion must communicate state and must never slow down repeated operations.

## Accessibility & Inclusion

Target WCAG AA for contrast, focus visibility, semantic structure, and keyboard access. Respect reduced motion. Use text, icons, and semantic status color together so failures are not communicated by color alone.
