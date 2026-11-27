#import <Foundation/Foundation.h>
#import <Vision/Vision.h>

int main(int argc, char **argv) {
    @autoreleasepool {
        NSData *image = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
        NSString *language = argc > 1 ? @(argv[1]) : @"rus+eng";
        NSArray<NSString *> *wanted = [language isEqualToString:@"eng"] ? @[@"en-US"] :
            [language isEqualToString:@"rus"] ? @[@"ru-RU"] : @[@"en-US", @"ru-RU"];

        VNRecognizeTextRequest *request = [[VNRecognizeTextRequest alloc] init];
        request.recognitionLevel = VNRequestTextRecognitionLevelAccurate;
        request.usesLanguageCorrection = NO;
        NSError *error = nil;
        NSArray<NSString *> *supported = [request supportedRecognitionLanguagesAndReturnError:&error];
        for (NSString *lang in wanted) {
            if (![supported containsObject:lang]) {
                return 2;
            }
        }
        request.recognitionLanguages = wanted;

        VNImageRequestHandler *handler = [[VNImageRequestHandler alloc] initWithData:image options:@{}];
        if (![handler performRequests:@[request] error:&error]) {
            return 1;
        }

        NSRegularExpression *timestamp = [NSRegularExpression
            regularExpressionWithPattern:@"(?:[01]?[0-9]|2[0-3]):[0-5][0-9]" options:0 error:NULL];
        NSRegularExpression *wordPattern = [NSRegularExpression
            regularExpressionWithPattern:@"\\S+" options:0 error:NULL];
        NSMutableArray *lines = [NSMutableArray array];
        for (VNRecognizedTextObservation *observation in request.results) {
            VNRecognizedText *candidate = [observation topCandidates:1].firstObject;
            if (!candidate) {
                continue;
            }
            CGRect box = observation.boundingBox;
            NSMutableDictionary *line = [@{
                @"text": candidate.string,
                @"box": @[@(CGRectGetMinX(box)), @(1 - CGRectGetMaxY(box)),
                          @(CGRectGetMaxX(box)), @(1 - CGRectGetMinY(box))]
            } mutableCopy];
            NSString *text = candidate.string;
            NSTextCheckingResult *match = [timestamp firstMatchInString:text options:0
                range:NSMakeRange(0, text.length)];
            if (match && match.range.location > 0) {
                NSRange prefix = NSMakeRange(0, match.range.location);
                while (prefix.length > 0 && [[NSCharacterSet whitespaceCharacterSet]
                       characterIsMember:[text characterAtIndex:prefix.length - 1]]) {
                    prefix.length--;
                }
                if (prefix.length > 0 && prefix.length <= 24) {
                    VNRectangleObservation *prefixBounds = [candidate boundingBoxForRange:prefix error:NULL];
                    if (prefixBounds) {
                        CGRect r = prefixBounds.boundingBox;
                        line[@"prefix_box"] = @[@(CGRectGetMinX(r)), @(1 - CGRectGetMaxY(r)),
                                                @(CGRectGetMaxX(r)), @(1 - CGRectGetMinY(r))];
                    }
                }
            }
            NSMutableArray *words = [NSMutableArray array];
            for (NSTextCheckingResult *word in [wordPattern matchesInString:text options:0
                 range:NSMakeRange(0, text.length)]) {
                VNRectangleObservation *bounds = [candidate boundingBoxForRange:word.range error:NULL];
                if (bounds) {
                    CGRect r = bounds.boundingBox;
                    [words addObject:@[@(CGRectGetMinX(r)), @(1 - CGRectGetMaxY(r)),
                                       @(CGRectGetMaxX(r)), @(1 - CGRectGetMinY(r))]];
                } else {
                    [words addObject:[NSNull null]];
                }
            }
            line[@"words"] = words;
            [lines addObject:line];
        }
        NSData *result = [NSJSONSerialization dataWithJSONObject:lines options:0 error:&error];
        if (!result) {
            return 1;
        }
        [[NSFileHandle fileHandleWithStandardOutput] writeData:result];
        return 0;
    }
}
