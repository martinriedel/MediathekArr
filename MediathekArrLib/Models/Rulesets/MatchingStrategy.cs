namespace MediathekArr.Models.Rulesets;

public enum MatchingStrategy
{
    SeasonAndEpisodeNumber, // Use season + episode number for matching
    ByAbsoluteEpisodeNumber, // TODO remove this in a later version
    AbsoluteEpisodeNumber, // Use absolute episode number for matching 
    ItemTitleIncludes,      // Match episodes where the tvdb episode name contains this title
    ItemTitleExact,          // Match episodes with an exact itemTitle
    ItemTitleEqualsAirdate
}
