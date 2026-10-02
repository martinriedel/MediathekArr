using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;
using System.Xml.Linq;

namespace MediathekArr.Utilities;

public partial class SubtitleConverter
{
    private static readonly XNamespace TtmlNamespace = "http://www.w3.org/ns/ttml";

    public static string? ConvertToSrt(string content)
    {
        if (content.TrimStart().StartsWith("WEBVTT"))
            return ConvertVttToSrt(content);

        return ConvertTtmlToSrt(content);
    }

    private static string? ConvertTtmlToSrt(string xmlContent)
    {
        var doc = XDocument.Parse(xmlContent);
        var paragraphs = doc.Descendants(TtmlNamespace + "p")
            .Where(p => p.Attribute("begin") != null && p.Attribute("end") != null)
            .ToList();

        if (paragraphs.Count == 0)
        {
            return null;
        }

        var srtBuilder = new StringBuilder();
        var index = 1;

        foreach (var paragraph in paragraphs)
        {
            var startTime = FormatTtmlTimestamp(paragraph.Attribute("begin")!.Value);
            var endTime = FormatTtmlTimestamp(paragraph.Attribute("end")!.Value);
            var text = NormalizeText(ExtractText(paragraph));

            if (string.IsNullOrWhiteSpace(text)) continue;

            srtBuilder.AppendLine(index.ToString())
                      .AppendLine($"{startTime} --> {endTime}")
                      .AppendLine(text)
                      .AppendLine();
            index++;
        }

        return srtBuilder.ToString().Trim();
    }

    private static string? ConvertVttToSrt(string vttContent)
    {
        var lines = vttContent.Split(["\r\n", "\n", "\r"], StringSplitOptions.None);
        var srtBuilder = new StringBuilder();
        var index = 1;
        var lineIndex = 0;

        // Skip WEBVTT header and any metadata lines until first empty line or cue
        while (lineIndex < lines.Length && !IsVttTimestampLine(lines[lineIndex]))
        {
            lineIndex++;
        }

        while (lineIndex < lines.Length)
        {
            if (string.IsNullOrWhiteSpace(lines[lineIndex]) || !IsVttTimestampLine(lines[lineIndex]))
            {
                lineIndex++;
                continue;
            }

            var timestampLine = lines[lineIndex];
            var convertedTimestamp = ConvertVttTimestampLine(timestampLine);
            if (convertedTimestamp == null)
            {
                lineIndex++;
                continue;
            }

            lineIndex++;

            var textLines = new List<string>();
            while (lineIndex < lines.Length && !string.IsNullOrWhiteSpace(lines[lineIndex]))
            {
                var cleanedLine = CleanVttTags(lines[lineIndex]);
                if (!string.IsNullOrWhiteSpace(cleanedLine))
                {
                    textLines.Add(cleanedLine);
                }
                lineIndex++;
            }

            if (textLines.Count == 0) continue;

            srtBuilder.AppendLine(index.ToString())
                      .AppendLine(convertedTimestamp)
                      .AppendLine(string.Join("\n", textLines))
                      .AppendLine();
            index++;
        }

        if (index == 1) return null;

        return srtBuilder.ToString().Trim();
    }

    private static bool IsVttTimestampLine(string line)
    {
        return line.Contains("-->");
    }

    private static string? ConvertVttTimestampLine(string line)
    {
        // "00:00:01.000 --> 00:00:04.000 align:middle" or "00:01.000 --> 00:04.000"
        var arrowIndex = line.IndexOf("-->", StringComparison.Ordinal);
        if (arrowIndex < 0) return null;

        var startPart = line[..arrowIndex].Trim();
        var afterArrow = line[(arrowIndex + 3)..].Trim();

        var endPart = afterArrow.Split(' ', StringSplitOptions.RemoveEmptyEntries)[0];

        var srtStart = ConvertVttTimestamp(startPart);
        var srtEnd = ConvertVttTimestamp(endPart);

        return $"{srtStart} --> {srtEnd}";
    }

    private static string ConvertVttTimestamp(string timestamp)
    {
        // VTT allows "MM:SS.mmm" (no hours) or "HH:MM:SS.mmm"
        // SRT requires "HH:MM:SS,mmm"
        var colonCount = timestamp.Count(c => c == ':');
        if (colonCount == 1)
        {
            // MM:SS.mmm → 00:MM:SS,mmm
            timestamp = "00:" + timestamp;
        }

        return timestamp.Replace('.', ',');
    }

    private static string CleanVttTags(string text)
    {
        return CleanVttTagsRegex().Replace(text, "").Trim();
    }

    private static string ExtractText(XElement element)
    {
        var sb = new StringBuilder();

        foreach (var node in element.Nodes())
        {
            switch (node)
            {
                case XText textNode:
                    sb.Append(textNode.Value.Replace("<br/>", "\n"));
                    break;
                case XElement child when child.Name.LocalName == "br":
                    sb.Append('\n');
                    break;
                case XElement child:
                    sb.Append(ExtractText(child));
                    break;
            }
        }

        return sb.ToString();
    }

    /// <summary>
    /// Formats an offset-time format (e.g., "14.40s") to clock-time format (e.g., "00:00:14.400")
    /// </summary>
    /// <param name="timestamp"></param>
    /// <returns></returns>
    private static string FormatTtmlTimestamp(string timestamp)
    {
        if (timestamp.EndsWith('s'))
        {
            var totalSeconds = decimal.Parse(timestamp.TrimEnd('s'), CultureInfo.InvariantCulture);
            var totalMilliseconds = (long)(totalSeconds * 1000m);
            var timeSpan = TimeSpan.FromMilliseconds(totalMilliseconds);
            return $"{(int)timeSpan.TotalHours:D2}:{timeSpan.Minutes:D2}:{timeSpan.Seconds:D2},{timeSpan.Milliseconds:D3}";
        }

        return timestamp.Replace('.', ',');
    }

    private static string NormalizeText(string text)
    {
        var lines = text.Split('\n')
            .Select(line => line.Trim())
            .Where(line => line.Length > 0);

        return string.Join("\n", lines);
    }

    [GeneratedRegex(@"<[^>]+>")]
    private static partial Regex CleanVttTagsRegex();
}
