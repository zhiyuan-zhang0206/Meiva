# Remove Grok client support and the deep research skill

The owner requested removal of Grok Bot support and the deep research skill.
At this checkpoint main already has no Grok harness implementation or Harness
enum member. Remove its remaining external-client skill publication support.
Keep xAI API model references, which describe a separate provider capability.

Remove the built-in meiva-deep-research skill and its audit helper/tests.
The former project-local deep-research directory is already absent.
Independent research examples in dynamic workflow remain useful examples;
historical decisions retain their original evidence.
