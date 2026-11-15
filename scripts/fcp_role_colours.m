// Where fcpxml/palette.py's role colours come from (0.34).
//
// Final Cut Pro builds a role's colours in code (FFRoleColorScheme), not in a
// plist or an asset catalog, so they are read the way Final Cut reads them:
// load its Flexo.framework into this small tool (no window, no Final Cut
// process), take each built-in main role (FFRole +builtInMainRoles), and ask
// FFColorForRole +colorSchemeForIndex: for the role's colour index. Prints
// one tab-separated row per role: name, index, then the scheme's colours as
// sRGB hex. palette.ROLE_SOURCE names the column the palette uses (one set
// in both themes since 0.34 round 4: roleFocusActiveColor).
//
//   clang -fobjc-arc -framework Foundation -framework AppKit \
//     -o /tmp/fcp_role_colours scripts/fcp_role_colours.m
//   DYLD_FRAMEWORK_PATH="/Applications/Final Cut Pro.app/Contents/Frameworks" /tmp/fcp_role_colours
//
// Read on Final Cut Pro 12.4 on 2026-11-15. tests/test_palette.py compiles
// and runs this when Final Cut and clang are installed, and fails when a
// token differs from what the app says.

#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#import <dlfcn.h>
#import <objc/message.h>
#import <objc/runtime.h>

static NSString *hex(id c) {
  if (![c isKindOfClass:[NSColor class]]) return @"-";
  NSColor *s = [(NSColor *)c colorUsingColorSpace:[NSColorSpace sRGBColorSpace]];
  if (!s) return @"-";
  return [NSString stringWithFormat:@"#%02X%02X%02X", (int)lround(s.redComponent * 255),
                                    (int)lround(s.greenComponent * 255), (int)lround(s.blueComponent * 255)];
}

int main(int argc, char **argv) {
  @autoreleasepool {
    NSString *app = argc > 1 ? @(argv[1]) : @"/Applications/Final Cut Pro.app";
    NSString *flexo = [app stringByAppendingPathComponent:@"Contents/Frameworks/Flexo.framework/Versions/A/Flexo"];
    if (!dlopen(flexo.fileSystemRepresentation, RTLD_LAZY | RTLD_GLOBAL)) {
      fprintf(stderr, "cannot load %s: %s\n", flexo.UTF8String, dlerror());
      return 2;
    }
    Class role = objc_getClass("FFRole"), forRole = objc_getClass("FFColorForRole");
    if (!role || !forRole) {
      fprintf(stderr, "FFRole / FFColorForRole not found in this Final Cut\n");
      return 3;
    }
    NSArray *columns = @[ @"baseColor", @"itemBackgroundColor", @"roleFocusActiveColor", @"audioWaveformColor" ];
    printf("role\tindex");
    for (NSString *col in columns) printf("\t%s", col.UTF8String);
    printf("\n");
    NSArray *roles = ((id (*)(id, SEL))objc_msgSend)(role, sel_registerName("builtInMainRoles"));
    for (id r in roles) {
      NSString *name = ((id (*)(id, SEL))objc_msgSend)(r, sel_registerName("displayName"));
      long index = ((long (*)(id, SEL))objc_msgSend)(r, sel_registerName("colorIndex"));
      id scheme = ((id (*)(id, SEL, long))objc_msgSend)(forRole, sel_registerName("colorSchemeForIndex:"), index);
      printf("%s\t%ld", name.UTF8String, index);
      for (NSString *col in columns) {
        id colour = ((id (*)(id, SEL))objc_msgSend)(scheme, NSSelectorFromString(col));
        printf("\t%s", hex(colour).UTF8String);
      }
      printf("\n");
    }
  }
  return 0;
}
